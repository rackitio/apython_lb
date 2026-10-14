import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics';

// Update these before running against your own deployment -- the defaults
// below are placeholders, not a real target. Override at runtime instead
// of editing this file with:
//   k6 run -e BASE_URL=https://your-host -e HOST_HEADER=your.host.header -e BODY_CONTAINS=some-text scripts/k6.js
//
// BASE_URL: wherever apython_lb is reachable from where k6 runs -- a
// ClusterIP/Service DNS name if running k6 inside the same cluster (e.g.
// the bundled docs/k8s/deploy/dev-nodeport-apython-lb.yaml Service is
// named "apython-lb" in the "default" namespace), or a public hostname.
// HOST_HEADER: the Host header for whichever backend config you want this
// run to exercise (see the README's "Add backends via curl" example).
// BODY_CONTAINS: a string that should appear in that backend's response,
// to confirm the proxied request actually reached it and got real content
// back, not just any 200.
const BASE_URL = __ENV.BASE_URL || 'https://apython-lb.default.svc.cluster.local';
const HOST_HEADER = __ENV.HOST_HEADER || 'your.host.header.example.com';
const BODY_CONTAINS = __ENV.BODY_CONTAINS || 'expected-text-from-your-backend';

const homepageDuration = new Trend('homepage_duration');

export const options = {
  insecureSkipTLSVerify: true,
  stages: [
    { duration: '30s', target: 1500 },
    { duration: '1m',  target: 1500 },
    { duration: '30s', target: 20  },
  ],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<2000'],
  },
};

export default function () {
  const url = `${BASE_URL}/`;

  const params = {
    headers: {
      Host: HOST_HEADER,
    },
  };

  const response = http.get(url, params);

  homepageDuration.add(response.timings.duration);

  check(response, {
    'status is 200': (r) => r.status === 200,
    'body contains expected text': (r) => r.body && r.body.includes(BODY_CONTAINS),
  });

  sleep(Math.random() * 2 + 1);
}
