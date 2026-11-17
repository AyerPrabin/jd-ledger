// k6 load test: many concurrent transfers that fight over a few hot accounts.
//
// Run from the repo root after python -m load.seed and with the API up:
//   k6 run load/transfers.js
//
// About 70% of transfers go between the 4 hot accounts, in random direction,
// so A->B and B->A run at the same moment and queue on the same row locks.
// About 10% of iterations send 3 identical copies of one transfer at once
// (same token, same Idempotency-Key) and check that exactly one is applied.
// load/verify.py then checks the database afterwards.

import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';
import exec from 'k6/execution';
import { textSummary } from 'https://jslib.k6.io/k6-summary/0.0.2/index.js';

const seed = JSON.parse(open('./out/seed.json'));
const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const PEAK_VUS = parseInt(__ENV.PEAK_VUS || '200', 10);
const DUP_COPIES = 3;
const ALL = seed.hot.concat(seed.cold);

const created = new Counter('transfers_created'); // 201: a new transfer was applied
const replayed = new Counter('transfers_replayed'); // 200: idempotent replay, nothing moved
const insufficient = new Counter('transfers_insufficient'); // 422: expected, rolled back
const noResponse = new Counter('transfers_no_response'); // status 0: outcome unknown
const unexpected = new Counter('unexpected_status'); // anything else (5xx, 4xx)
const dupGroups = new Counter('dup_groups');
const dupApplied = new Counter('dup_groups_applied'); // groups where exactly one copy was applied
const dupTwice = new Counter('dup_applied_more_than_once');

// 422 is a normal answer (not enough points), so it does not count as failed.
http.setResponseCallback(http.expectedStatuses(200, 201, 422));

export const options = {
  scenarios: {
    transfers: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '10s', target: PEAK_VUS },
        { duration: '45s', target: PEAK_VUS },
        { duration: '5s', target: 0 },
      ],
      gracefulRampDown: '10s',
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    checks: ['rate>0.99'],
    unexpected_status: ['count==0'],
    dup_applied_more_than_once: ['count==0'],
    http_req_duration: ['p(95)<5000'],
  },
};

function pick(list) {
  return list[Math.floor(Math.random() * list.length)];
}

function pair() {
  const pool = Math.random() < 0.7 ? seed.hot : ALL;
  const from = pick(pool);
  let to = pick(pool);
  while (to.id === from.id) to = pick(pool);
  return [from, to];
}

function request(from, to, amount, key) {
  return {
    method: 'POST',
    url: `${BASE}/transfers`,
    body: JSON.stringify({ from_account_id: from.id, to_account_id: to.id, amount }),
    params: {
      headers: {
        Authorization: `Bearer ${from.token}`,
        'Content-Type': 'application/json',
        'Idempotency-Key': key,
      },
      tags: { name: 'POST /transfers' },
    },
  };
}

function count(res) {
  if (res.status === 201) created.add(1);
  else if (res.status === 200) replayed.add(1);
  else if (res.status === 422) insufficient.add(1);
  else if (res.status === 0) noResponse.add(1);
  else unexpected.add(1);
}

function single(key) {
  const [from, to] = pair();
  const req = request(from, to, 1 + Math.floor(Math.random() * 5), key);
  const res = http.request(req.method, req.url, req.body, req.params);
  count(res);
  check(res, { 'transfer is 201 or 422': (r) => r.status === 201 || r.status === 422 });
}

function duplicates(key) {
  const [from, to] = pair();
  const amount = 1 + Math.floor(Math.random() * 5);
  const copies = [];
  for (let i = 0; i < DUP_COPIES; i++) copies.push(request(from, to, amount, key));
  const responses = http.batch(copies);
  responses.forEach(count);
  dupGroups.add(1);

  const statuses = responses.map((r) => r.status);
  const applied = responses.filter((r) => r.status === 201);
  if (applied.length === 1) dupApplied.add(1);
  if (applied.length > 1) dupTwice.add(1);
  const opId = applied.length === 1 ? applied[0].json('id') : null;
  check(statuses, {
    'duplicates: every copy answered 200, 201 or 422': (s) =>
      s.every((x) => x === 200 || x === 201 || x === 422),
    'duplicates: applied at most once': () => applied.length <= 1,
    // the hot accounts are funded far beyond what the test moves, so with
    // no 422 in the group exactly one copy must have done the work
    'duplicates: applied exactly once': (s) => s.includes(422) || applied.length === 1,
  });
  check(responses, {
    'duplicates: replays return the same operation': (rs) =>
      rs
        .filter((r) => r.status === 200)
        .every((r) => r.headers['Idempotent-Replayed'] === 'true' && r.json('id') === opId),
  });
}

export default function () {
  const key = `${exec.vu.idInTest}-${exec.vu.iterationInScenario}`;
  if (Math.random() < 0.1) duplicates(`dup-${key}`);
  else single(`t-${key}`);
}

export function handleSummary(data) {
  return {
    stdout: textSummary(data, { indent: ' ', enableColors: false }),
    'load/out/summary.json': JSON.stringify(data, null, 2),
  };
}
