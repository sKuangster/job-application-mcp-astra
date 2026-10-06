'use strict';
// Canned responses conforming to docs/PROTOCOL.md §4.

const NOW = Date.now() / 1000;
const XSS = '<img src=x onerror=alert(1)>';

const status = {
  control: 'RUNNING',
  mode: 'backlog',
  workers: [
    { worker_id: 'browser-1', pool: 'browser', status: 'busy', application_id: 'app-101', company: 'Acme Robotics',
      title: 'Software Engineer Intern', state: 'FILLING', activity: 'Filling 13 fields', since: NOW - 95 },
    { worker_id: 'browser-2', pool: 'browser', status: 'busy', application_id: 'app-102', company: 'Globex',
      title: 'Data Analyst', state: 'ANSWERING', activity: '', since: NOW - 30 },
    { worker_id: 'prep-1', pool: 'prep', status: 'idle', application_id: null, company: null, title: null, state: null,
      activity: null, since: null }
  ],
  limits: { browser_workers: 2, prep_workers: 1 },
  counts: { QUEUED: 120, SUBMITTED: 18, NEEDS_USER: 1 },
  queue_remaining: 127,
  today: { submitted: 18, skipped: 2, ineligible: 5, needs_user: 1, failed: 3, active: 2 },
  needs_user: [
    { application_id: 'app-099', company: 'Initech ' + XSS, title: 'Backend Developer', reason: 'CAPTCHA detected on submit page',
      kind: 'captcha', instructions: 'Solve the hCaptcha in the focused tab; ApplyPilot resumes automatically.', since: NOW - 300 }
  ],
  pending_reasoning: 3,
  bridge: { connected: true, last_seen: NOW - 1, owned_tabs: 2 },
  config: { ready: true, missing: [] },
  recent_errors: [{ ts: NOW - 600, application_id: 'app-090', message: 'Workday dropdown did not accept value' }],
  version: '0.1.0'
};

function summary(overrides) {
  return Object.assign({
    id: 'app-101', job_id: 'job-55', company: 'Acme Robotics', title: 'Software Engineer Intern', location: 'Remote, US',
    state: 'NEEDS_USER', state_reason: 'Missing fact: graduation date', ats: 'greenhouse', resume: 'swe_resume.pdf',
    domain: 'boards.greenhouse.io', simplify_url: 'https://simplify.jobs/p/abc', application_url: 'https://boards.greenhouse.io/acme/jobs/1',
    current_url: 'https://boards.greenhouse.io/acme/jobs/1#app', created_at: NOW - 3600, updated_at: NOW - 60,
    submitted_at: null, attempts: 2, needs_user: { kind: 'missing_fact', reason: 'Graduation date unknown', instructions: 'Enter your expected graduation date.' },
    worker_id: 'browser-1', eligibility_decision: 'eligible', failure: null
  }, overrides || {});
}

const methods = ['deterministic', 'fact', 'preference', 'eeo', 'template', 'reused', 'generated', 'user', 'default', 'research'];
const answers = methods.map((m, i) => ({
  field_key: 'f_key' + i,
  question: 'Question number ' + i + ' (' + m + ')',
  normalized: 'question ' + i,
  qclass: i % 2 ? 'free_text' : 'choice',
  field_type: m === 'generated' ? 'textarea' : 'text',
  options: null,
  required: true,
  limit: m === 'generated' ? 2000 : null,
  value: m === 'generated' ? 'Paragraph one.\n\nParagraph two ' + XSS : 'Value ' + i,
  method: m,
  source: m === 'fact' ? 'candidate.identity.first_name' : m === 'template' ? 'template:why_company' : 'src-' + m,
  confidence: 0.9,
  status: 'filled',
  page_index: 0,
  updated_at: NOW - 100 + i
}));
answers.push({
  field_key: 'f_grad', question: 'Expected graduation date', normalized: 'graduation date', qclass: 'date', field_type: 'month',
  options: null, required: true, limit: null, value: null, method: 'default', source: null, confidence: null, status: 'missing',
  page_index: 1, updated_at: NOW - 50
});

const components = ['worker', 'browser', 'answers', 'eligibility', 'browser'];
const events = [];
for (let i = 1; i <= 30; i++) {
  events.push({
    id: i, ts: NOW - 1000 + i * 10, application_id: 'app-101', job_id: 'job-55', worker_id: 'browser-1',
    component: components[i % components.length], event_type: 'evt_' + i,
    level: i === 7 ? 'warning' : i === 21 ? 'error' : 'info',
    message: 'Event message ' + i, data: i % 3 === 0 ? { step: i, url: 'https://example.com/' + i } : {}
  });
}

const detail = {
  summary: summary(),
  job: { url: 'https://acme.example/jobs/1', company: 'Acme Robotics', title: 'Software Engineer Intern', description: 'Build robots.' },
  eligibility: { decision: 'eligible', reasoning: 'Internship, US remote, no sponsorship needed.', matched_rules: ['role.intern', 'location.us'] },
  resume: { file: 'swe_resume.pdf', category: 'software', reason: 'Role title matches software engineering.' },
  answers,
  research: [{ id: 'r1', topic: 'Acme mission', summary: 'Acme builds warehouse robots.', sources: [{ url: 'https://acme.example/about', title: 'About Acme', fetched_at: NOW - 2000 }], created_at: NOW - 2000 }],
  // shuffled to verify chronological ordering
  events: events.slice(15).concat(events.slice(0, 15)),
  errors: [events[6], events[20]],
  pages: [{ url: 'https://boards.greenhouse.io/acme/jobs/1', kind: 'form', fingerprint: 'abc', ts: NOW - 900 }],
  submission: null,
  metrics: { duration_seconds: 245, model_calls: 3 }
};

const metrics = {
  applications_completed: 18,
  time_per_application_avg_seconds: 312.5,
  time_per_application_median_seconds: 280,
  page_wait_seconds_avg: 2.4,
  reasoning_calls: 41,
  research_calls: 7,
  retries: 4,
  answers_by_method: { deterministic: 120, fact: 80, generated: 12 },
  submitted_per_day: [{ date: '2026-09-30', value: 4 }, { date: '2026-10-01', value: 9 }, { date: '2026-10-02', value: 5 }]
};

const appList = {
  items: [summary({ id: 'app-101' }), summary({ id: 'app-102', company: 'Globex', state: 'FAILED', failure: 'TIMEOUT' })],
  total: 120
};

module.exports = { NOW, XSS, status, summary, answers, events, detail, metrics, appList };
