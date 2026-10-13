// Same profiles/branches as fixtures/session-hub.test-config.json, in GET /config form.
import { applyHubConfig } from '../../plugins/session-hub/desktop/plugin.js'

export const TEST_HUB_CONFIG = {
  contract: 'session-hub-config-v1',
  profilesMode: 'explicit',
  localProfiles: ['default', 'ops', 'review', 'builder', 'search'],
  remoteProfiles: ['X/default', 'Y/default', 'Z/default'],
  profileLabels: {
    default: 'Lead', 'ops': 'Ops', search: 'Search', builder: 'Dev', review: 'Review',
    'X/default': 'Branch X (X)', 'Y/default': 'Branch Y (Y)', 'Z/default': 'Branch Z (Z)'
  },
  instancesEnabled: true,
  localLabel: '본사',
  remoteGroupLabel: '지사',
  remoteInstances: { K: { label: 'Branch X' }, H: { label: 'Branch Y' }, M: { label: 'Branch Z' } }
}

applyHubConfig(TEST_HUB_CONFIG)
