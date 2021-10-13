import logging
from collections import defaultdict
from typing import Any, Mapping, Optional, Tuple
from uuid import uuid4

import sentry_sdk
from requests import Request
from rest_framework.exceptions import ParseError
from rest_framework.response import Response

from sentry import eventstream, features, search
from sentry.api.base import audit_logger
from sentry.api.issue_search import convert_query_values, parse_search_query
from sentry.api.serializers import serialize
from sentry.app import ratelimiter
from sentry.constants import DEFAULT_SORT_OPTION
from sentry.exceptions import InvalidSearchQuery
from sentry.models import Environment, Group, GroupHash, GroupStatus, Project, Release
from sentry.models.group import looks_like_short_id
from sentry.models.groupinbox import GroupInbox
from sentry.signals import advanced_search_feature_gated, issue_deleted
from sentry.tasks.deletion import delete_groups as delete_groups_task
from sentry.utils import metrics
from sentry.utils.audit import create_audit_entry
from sentry.utils.compat import zip
from sentry.utils.cursors import Cursor, CursorResult
from sentry.utils.hashlib import md5_text

from .validators import ValidationError

delete_logger = logging.getLogger("sentry.deletions.api")


def build_query_params_from_request(request, organization, projects, environments):
    query_kwargs = {"projects": projects, "sort_by": request.GET.get("sort", DEFAULT_SORT_OPTION)}

    limit = request.GET.get("limit")
    if limit:
        try:
            query_kwargs["limit"] = int(limit)
        except ValueError:
            raise ValidationError("invalid limit")

    # TODO: proper pagination support
    if request.GET.get("cursor"):
        try:
            query_kwargs["cursor"] = Cursor.from_string(request.GET.get("cursor"))
        except ValueError:
            raise ParseError(detail="Invalid cursor parameter.")
    query = request.GET.get("query", "is:unresolved").strip()
    sentry_sdk.set_tag("search.query", query)
    sentry_sdk.set_tag("search.sort", query)
    if projects:
        sentry_sdk.set_tag("search.projects", len(projects) if len(projects) <= 5 else ">5")
    if environments:
        sentry_sdk.set_tag(
            "search.environments", len(environments) if len(environments) <= 5 else ">5"
        )
    if query:
        try:
            search_filters = convert_query_values(
                parse_search_query(query), projects, request.user, environments
            )
        except InvalidSearchQuery as e:
            raise ValidationError(f"Error parsing search query: {e}")

        validate_search_filter_permissions(organization, search_filters, request.user)
        query_kwargs["search_filters"] = search_filters

    return query_kwargs


# List of conditions that mark a SearchFilter as an advanced search. Format is
# (lambda SearchFilter(): <boolean condition>, '<feature_name')
advanced_search_features = [
    (lambda search_filter: search_filter.is_negation, "negative search"),
    (lambda search_filter: search_filter.value.is_wildcard(), "wildcard search"),
]


def validate_search_filter_permissions(organization, search_filters, user):
    """
    Verifies that an organization is allowed to perform the query that they
    submitted.
    If the org is using a feature they don't have access to, raises
    `ValidationError` with information which part of the query they don't have
    access to.
    :param search_filters:
    """
    # If the organization has advanced search, then no need to perform any
    # other checks since they're allowed to use all search features
    if features.has("organizations:advanced-search", organization):
        return

    for search_filter in search_filters:
        for feature_condition, feature_name in advanced_search_features:
            if feature_condition(search_filter):
                advanced_search_feature_gated.send_robust(
                    user=user, organization=organization, sender=validate_search_filter_permissions
                )
                raise ValidationError(
                    f"You need access to the advanced search feature to use {feature_name}"
                )


def get_by_short_id(organization_id, is_short_id_lookup, query):
    if is_short_id_lookup == "1" and looks_like_short_id(query):
        try:
            return Group.objects.by_qualified_short_id(organization_id, query)
        except Group.DoesNotExist:
            pass


def delete_group_list(request, project, group_list, delete_type):
    if not group_list:
        return

    # deterministic sort for sanity, and for very large deletions we'll
    # delete the "smaller" groups first
    group_list.sort(key=lambda g: (g.times_seen, g.id))
    group_ids = [g.id for g in group_list]

    Group.objects.filter(id__in=group_ids).exclude(
        status__in=[GroupStatus.PENDING_DELETION, GroupStatus.DELETION_IN_PROGRESS]
    ).update(status=GroupStatus.PENDING_DELETION)

    eventstream_state = eventstream.start_delete_groups(project.id, group_ids)
    transaction_id = uuid4().hex

    # We do not want to delete split hashes as they are necessary for keeping groups... split.
    GroupHash.objects.filter(
        project_id=project.id, group__id__in=group_ids, state=GroupHash.State.SPLIT
    ).update(group=None)
    GroupHash.objects.filter(project_id=project.id, group__id__in=group_ids).exclude(
        state=GroupHash.State.SPLIT
    ).delete()

    # We remove `GroupInbox` rows here so that they don't end up influencing queries for
    # `Group` instances that are pending deletion
    GroupInbox.objects.filter(project_id=project.id, group__id__in=group_ids).delete()

    delete_groups_task.apply_async(
        kwargs={
            "object_ids": group_ids,
            "transaction_id": transaction_id,
            "eventstream_state": eventstream_state,
        },
        countdown=3600,
    )

    for group in group_list:
        create_audit_entry(
            request=request,
            transaction_id=transaction_id,
            logger=audit_logger,
            organization_id=project.organization_id,
            target_object=group.id,
        )

        delete_logger.info(
            "object.delete.queued",
            extra={
                "object_id": group.id,
                "organization_id": project.organization_id,
                "transaction_id": transaction_id,
                "model": type(group).__name__,
            },
        )

        issue_deleted.send_robust(
            group=group, user=request.user, delete_type=delete_type, sender=delete_group_list
        )


def delete_groups(request, projects, organization_id, search_fn):
    """
    `search_fn` refers to the `search.query` method with the appropriate
    project, org, environment, and search params already bound
    """
    group_ids = request.GET.getlist("id")
    if group_ids:
        group_list = list(
            Group.objects.filter(
                project__in=projects,
                project__organization_id=organization_id,
                id__in=set(group_ids),
            ).exclude(status__in=[GroupStatus.PENDING_DELETION, GroupStatus.DELETION_IN_PROGRESS])
        )
    else:
        try:
            # bulk mutations are limited to 1000 items
            # TODO(dcramer): it'd be nice to support more than this, but its
            # a bit too complicated right now
            cursor_result, _ = search_fn({"limit": 1000, "paginator_options": {"max_limit": 1000}})
        except ValidationError as exc:
            return Response({"detail": str(exc)}, status=400)

        group_list = list(cursor_result)

    if not group_list:
        return Response(status=204)

    groups_by_project_id = defaultdict(list)
    for group in group_list:
        groups_by_project_id[group.project_id].append(group)

    for project in projects:
        delete_group_list(
            request, project, groups_by_project_id.get(project.id), delete_type="delete"
        )

    return Response(status=204)


def track_slo_response(name):
    def inner_func(function):
        def wrapper(request, *args, **kwargs):
            from sentry.utils import snuba

            try:
                response = function(request, *args, **kwargs)
            except snuba.RateLimitExceeded:
                metrics.incr(
                    f"{name}.slo.http_response",
                    sample_rate=1.0,
                    tags={
                        "status": 429,
                        "detail": "snuba.RateLimitExceeded",
                        "func": function.__qualname__,
                    },
                )
                raise
            except Exception:
                metrics.incr(
                    f"{name}.slo.http_response",
                    sample_rate=1.0,
                    tags={"status": 500, "detail": "Exception"},
                )
                # Continue raising the error now that we've incr the metric
                raise

            metrics.incr(
                f"{name}.slo.http_response",
                sample_rate=1.0,
                tags={"status": response.status_code, "detail": "response"},
            )
            return response

        return wrapper

    return inner_func


def build_rate_limit_key(function, request):
    ip = request.META["REMOTE_ADDR"]
    return f"rate_limit_endpoint:{md5_text(function.__qualname__).hexdigest()}:{ip}"


def rate_limit_endpoint(limit=1, window=1):
    def inner(function):
        def wrapper(self, request, *args, **kwargs):
            if ratelimiter.is_limited(
                build_rate_limit_key(function, request),
                limit=limit,
                window=window,
            ):
                return Response(
                    {
                        "detail": f"You are attempting to use this endpoint too quickly. Limit is {limit}/{window}s"
                    },
                    status=429,
                )
            else:
                return function(self, request, *args, **kwargs)

        return wrapper

    return inner


def calculate_stats_period(stats_period, start, end):
    if stats_period is None:
        # default
        stats_period = "24h"
    elif stats_period == "":
        # disable stats
        stats_period = None

    if stats_period == "auto":
        stats_period_start = start
        stats_period_end = end
    else:
        stats_period_start = None
        stats_period_end = None
    return stats_period, stats_period_start, stats_period_end


def prep_search(
    cls: Any,
    request: Request,
    project: Project,
    extra_query_kwargs: Optional[Mapping[str, Any]] = None,
) -> Tuple[Any, Mapping[str, Any]]:
    try:
        environment = cls._get_environment_from_request(request, project.organization_id)
    except Environment.DoesNotExist:
        # XXX: The 1000 magic number for `max_hits` is an abstraction leak
        # from `sentry.api.paginator.BasePaginator.get_result`.
        result = CursorResult([], None, None, hits=0, max_hits=1000)
        query_kwargs = {}
    else:
        environments = [environment] if environment is not None else environment
        query_kwargs = build_query_params_from_request(
            request, project.organization, [project], environments
        )
        if extra_query_kwargs is not None:
            assert "environment" not in extra_query_kwargs
            query_kwargs.update(extra_query_kwargs)

        query_kwargs["environments"] = environments
        result = search.query(**query_kwargs)
    return result, query_kwargs


def get_first_last_release(request, group):
    first_release = group.get_first_release()
    if first_release is not None:
        last_release = group.get_last_release()
    else:
        last_release = None

    if first_release is not None and last_release is not None:
        first_release, last_release = get_first_last_release_info(
            request, group, [first_release, last_release]
        )
    elif first_release is not None:
        first_release = get_release_info(request, group, first_release)
    elif last_release is not None:
        last_release = get_release_info(request, group, last_release)

    return first_release, last_release


def get_release_info(request, group, version):
    try:
        release = Release.objects.get(
            projects=group.project,
            organization_id=group.project.organization_id,
            version=version,
        )
    except Release.DoesNotExist:
        release = {"version": version}
    return serialize(release, request.user)


def get_first_last_release_info(request, group, versions):
    releases = {
        release.version: release
        for release in Release.objects.filter(
            projects=group.project,
            organization_id=group.project.organization_id,
            version__in=versions,
        )
    }
    serialized_releases = serialize(
        [releases.get(version) for version in versions],
        request.user,
    )
    # Default to a dictionary if the release object wasn't found and not serialized
    return [
        item if item is not None else {"version": version}
        for item, version in zip(serialized_releases, versions)
    ]
