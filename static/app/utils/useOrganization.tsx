import {useContext} from 'react';

import {OrganizationContext} from 'app/views/organizationContext';

export function useOrganization() {
  const organization = useContext(OrganizationContext);
  if (!organization) {
    throw new Error('useOrganization called but organization is not set.');
  }
  return organization;
}

export function useOrgSlug() {
  return useOrganization().slug;
}
