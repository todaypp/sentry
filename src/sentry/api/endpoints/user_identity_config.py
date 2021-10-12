import itertools
from typing import Iterable, Optional

from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from sentry.api.bases.user import UserEndpoint
from sentry.api.serializers import serialize
from sentry.api.serializers.models.user_identity_config import Status, UserIdentityConfig
from sentry.models import AuthIdentity, Identity, User
from social_auth.models import UserSocialAuth


def get_identities(user: User) -> Iterable[UserIdentityConfig]:
    """Get objects representing all of a user's identities.

    Fetch the identity-related objects from the database and set
    their associated statuses. This function is responsible for
    setting the status because it depends on the user state and the
    full set of available identities.
    """

    has_password = user.has_usable_password()
    global_identity_objs = list(Identity.objects.filter(user=user))

    def get_social_identities() -> Iterable[UserIdentityConfig]:
        return (
            UserIdentityConfig.wrap(obj, Status.CAN_DISCONNECT)
            for obj in UserSocialAuth.objects.filter(user=user)
        )

    def get_global_identities() -> Iterable[UserIdentityConfig]:
        # Allow global auth credentials to be deleted if the user has a
        # password or at least one other global identity to fall back on.
        # AuthIdentities don't count, because we don't want to risk the
        # user getting locked out of their profile if they're kicked from
        # the organization.
        global_id_status = (
            Status.CAN_DISCONNECT
            if (has_password or len(global_identity_objs) > 1)
            else Status.NEEDED_FOR_GLOBAL_AUTH
        )
        return (UserIdentityConfig.wrap(obj, global_id_status) for obj in global_identity_objs)

    def get_org_identity_status(obj: AuthIdentity) -> Status:
        if not obj.auth_provider.flags.allow_unlinked:
            return Status.NEEDED_FOR_ORG_AUTH
        elif has_password or len(global_identity_objs) > 0:
            return Status.CAN_DISCONNECT
        else:
            # Assume the user has only this AuthIdentity as their
            # means of logging in. (They might actually have two or
            # more from non-SSO-requiring orgs, but that's so rare
            # and weird that we don't care.)
            return Status.NEEDED_FOR_GLOBAL_AUTH

    def get_org_identities() -> Iterable[UserIdentityConfig]:
        return (
            UserIdentityConfig.wrap(obj, get_org_identity_status(obj))
            for obj in AuthIdentity.objects.filter(user=user)
        )

    return itertools.chain(
        get_social_identities(),
        get_global_identities(),
        get_org_identities(),
    )


class UserIdentityConfigEndpoint(UserEndpoint):
    def get(self, request, user):
        """
        Retrieve all of a user's SocialIdentity, Identity, and AuthIdentity values
        ``````````````````````````````````````````````````````````````````````````

        :pparam string user ID: user ID, or 'me'
        :auth: required
        """

        identities = list(get_identities(user))
        return Response(serialize(identities))


class UserIdentityConfigDetailsEndpoint(UserEndpoint):
    @staticmethod
    def _get_identity(user, category, identity_id) -> Optional[UserIdentityConfig]:
        identity_id = int(identity_id)

        # This fetches and iterates over all the user's identities.
        # If needed, we could optimize to look directly for the one
        # object, but we would still need to examine the full set of
        # Identity objects in order to correctly set the status.
        for identity in get_identities(user):
            if identity.category == category and identity.id == identity_id:
                return identity
        return None

    def get(self, request, user, category, identity_id):
        identity = self._get_identity(user, category, identity_id)
        if identity:
            return Response(serialize(identity))
        else:
            return Response(status=status.HTTP_404_NOT_FOUND)

    def delete(self, request, user, category, identity_id):
        with transaction.atomic():
            identity = self._get_identity(user, category, identity_id)
            if not identity:
                # Returns 404 even if the ID exists but belongs to
                # another user. In that case, 403 would also be
                # appropriate, but 404 is fine or even preferable.
                return Response(status=status.HTTP_404_NOT_FOUND)
            if identity.status != Status.CAN_DISCONNECT:
                return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

            model_type = identity.get_model_type_for_category()
            model_type.objects.get(id=int(identity_id)).delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
