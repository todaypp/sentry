import {
  IconFire,
  IconFix,
  IconInfo,
  IconLocation,
  IconMobile,
  IconRefresh,
  IconSpan,
  IconStack,
  IconSwitch,
  IconTerminal,
  IconUser,
  IconWarning,
} from 'app/icons';
import {BreadcrumbType} from 'app/types/breadcrumbs';

type Props = {
  type: BreadcrumbType;
};

function Icon({type}: Props) {
  switch (type) {
    case BreadcrumbType.USER:
    case BreadcrumbType.UI:
      return <IconUser size="xs" />;
    case BreadcrumbType.NAVIGATION:
      return <IconLocation size="xs" />;
    case BreadcrumbType.DEBUG:
      return <IconFix size="xs" />;
    case BreadcrumbType.INFO:
      return <IconInfo size="xs" />;
    case BreadcrumbType.ERROR:
      return <IconFire size="xs" />;
    case BreadcrumbType.HTTP:
      return <IconSwitch size="xs" />;
    case BreadcrumbType.WARNING:
      return <IconWarning size="xs" />;
    case BreadcrumbType.QUERY:
      return <IconStack size="xs" />;
    case BreadcrumbType.SYSTEM:
      return <IconMobile size="xs" />;
    case BreadcrumbType.SESSION:
      return <IconRefresh size="xs" />;
    case BreadcrumbType.TRANSACTION:
      return <IconSpan size="xs" />;
    default:
      return <IconTerminal size="xs" />;
  }
}

export default Icon;
