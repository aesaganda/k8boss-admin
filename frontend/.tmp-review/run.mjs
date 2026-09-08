import { formModelFor, localIssues, setIn, getIn } from './of_orig.mjs';

// Walk the exact clicks: Create DaemonSet, type "1" into Max unavailable,
// then flip Update strategy RollingUpdate -> OnDelete.
let doc = {
  apiVersion: 'apps/v1', kind: 'DaemonSet',
  metadata: { name: 'agent' },
  spec: {
    selector: { matchLabels: { app: 'agent' } },
    template: { metadata: { labels: { app: 'agent' } }, spec: { containers: [{ name: 'c', image: 'nginx' }] } },
  },
};
const model = formModelFor('apps/v1', 'DaemonSet');
doc = setIn(doc, ['spec', 'updateStrategy', 'rollingUpdate', 'maxUnavailable'], 1);  // typed "1"
doc = setIn(doc, ['spec', 'updateStrategy', 'type'], 'OnDelete');                    // select changed
console.log('document after clicks:', JSON.stringify(doc.spec.updateStrategy));
console.log(JSON.stringify(localIssues(doc, model), null, 1));

// And a StatefulSet for comparison
let sts = {
  apiVersion: 'apps/v1', kind: 'StatefulSet', metadata: { name: 'db' },
  spec: { serviceName: 'db', selector: { matchLabels: { app: 'db' } },
    template: { metadata: { labels: { app: 'db' } }, spec: { containers: [{ name: 'c', image: 'nginx' }] } },
    updateStrategy: { type: 'OnDelete', rollingUpdate: { partition: 2 } } },
};
console.log('STS:', JSON.stringify(localIssues(sts, formModelFor('apps/v1','StatefulSet')).map(i=>i.severity)));
