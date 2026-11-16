# Activity and audit logging

The CMS has two logging systems that feed protective monitoring and SIEM integration:

| System                    | What it captures                                                                      | Logger         | Stored in the database?                             |
| ------------------------- | ------------------------------------------------------------------------------------- | -------------- | --------------------------------------------------- |
| **Protective monitoring** | Denied access: 401 and 403 responses, and admin requests redirected to the login page | `cms.security` | No, stdout only                                     |
| **Audit logging**         | Content activity: create, edit, publish, delete, view and preview actions             | `cms.audit`    | Yes, in Wagtail's audit log, and mirrored to stdout |

Both write JSON to stdout, following the [ONS logging standards](https://github.com/ONSdigital/dp-standards/blob/main/LOGGING_STANDARDS.md) (see [Logging](integrations/logging.md)). The platform's log aggregation picks them up from there and sends them on to the SIEM, so the application doesn't need to know anything about the SIEM.

Login, logout and failed login events are logged separately. See [Related events](#related-events).

## Internal and external environments

The CMS is deployed twice, and `IS_EXTERNAL_ENV` tells the two apart (see [Deployment environments](custom-features/deployment-environments.md)):

- **Internal** (`IS_EXTERNAL_ENV=False`): the editorial CMS that ONS staff use. It runs the Wagtail admin and handles authentication. Local dev and tests run as internal too.
- **External** (`IS_EXTERNAL_ENV=True`): the public website. It has no Wagtail admin, no authentication backends, and the database router blocks writes to everything except image renditions.

Both are production deployments. `IS_EXTERNAL_ENV` does not mean "production", and setting it to `False` does not mean "dev". In production, the internal environment logs PII about staff and the external environment logs no PII about the public.

How this affects each system:

| Behaviour                                                        | Internal | External                                                                              |
| ---------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------- |
| `UnauthorizedAccessLoggingMiddleware` installed                  | Yes      | No                                                                                    |
| Denied access events include `email`, `internal_user_id`, `auth` | Yes      | n/a (middleware not installed; see below)                                             |
| Denied access events include `ip_address`                        | Yes      | n/a                                                                                   |
| Audit events written                                             | Yes      | No. There's no admin and no logged-in users, and log entry tables can't be written to |
| Login, logout and failed login events                            | Yes      | No (no authentication)                                                                |
| Gunicorn access logs include `http.ip_address`                   | Yes      | No                                                                                    |

Why the difference:

- **Internal users are known staff.** To investigate a denied request or an unexpected edit you need to know who did it and where from, so emails, user IDs and IP addresses are logged.
- **External users are members of the public.** We have no reason to log their IP addresses, and doing so would mean processing personal data under UK GDPR without a need. The external site also has no admin and no logins, so there's nothing for these systems to protect there. Running the middleware would only add noise (for example, a 403 from a bot).

The middleware checks `IS_EXTERNAL_ENV` itself before adding user details and IP addresses. In normal deployments it never runs in the external environment, so this check is defence in depth: if someone adds the middleware to the external stack by mistake, it still won't log PII. `cms.core.utils.get_client_ip()` follows the same rule more strictly and raises an error if it's called in the external environment.

## Protective monitoring

`UnauthorizedAccessLoggingMiddleware` in [`cms/core/middleware.py`](https://github.com/ONSdigital/dis-wagtail/tree/main/cms/core/middleware.py) logs denied requests to the `cms.security` logger at `WARNING` level. It's added to `MIDDLEWARE` in `cms/settings/base.py`, only when `IS_EXTERNAL_ENV` is false, and it sits after the auth middleware so `request.user` is set.

These events are deliberately not saved to the database.

### When an event is logged

A response counts as denied if any of these apply:

- **401 Unauthorized or 403 Forbidden**, on any URL.
- **302 redirect from an admin URL to the login page.** Wagtail redirects anonymous users, and users without admin access, to the login page rather than returning a 403. Both the Wagtail login URL (`WAGTAILADMIN_LOGIN_URL`) and Django's `LOGIN_URL` (used by `@login_required`) count.
- **302 redirect from an admin URL to the admin home, with Wagtail's permission denied message.** This is what Wagtail does when a logged-in editor opens a page they don't have permission for. The middleware checks for the queued "Sorry, you do not have permission to access this area." error message without consuming it.

### Exempt URLs

These admin URLs are skipped even when they redirect to the login page, because the redirect is expected and logging it would just be noise:

- `wagtailadmin_login`, `wagtailadmin_password_reset`, `wagtailadmin_logout`: when core login is disabled, these redirect to the external login service.
- `wagtailadmin_editing_sessions:ping` and `wagtailadmin_editing_sessions:release`: the page editor calls these in the background, including after the user's session expires or they log out in another tab.

Exemptions only apply to the login redirect check. A 401 or 403 from any URL is always logged.

### Event types

- `unauthenticated access attempt`: the user wasn't logged in.
- `unauthorized access attempt`: the user was logged in but didn't have permission.

### Example

```json
{
    "created_at": "2026-11-13T10:15:30.123Z",
    "namespace": "cms.security",
    "severity": 2,
    "event": "unauthorized access attempt",
    "classification": "PROTECTIVE_MONITORING",
    "http": {
        "host": "localhost:8000",
        "method": "GET",
        "path": "/admin/users/",
        "query": "",
        "scheme": "http",
        "started_at": "2026-11-13T10:15:30.100Z",
        "status_code": 302,
        "port": 8000
    },
    "auth": { "identity": "f1d2c3a4-...", "identity_type": "user" },
    "data": {
        "event_type": "unauthorized access attempt",
        "user_agent": "Mozilla/5.0 ...",
        "internal_user_id": 4,
        "email": "editor@example.com",
        "ip_address": "127.0.0.1"
    }
}
```

### Fields

See [Common fields](#common-fields) for `created_at`, `namespace`, `severity` and `event`.

| Field                   | Description                                                                                                                                 |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `classification`        | Always `PROTECTIVE_MONITORING`. Use this to filter these events in the SIEM                                                                 |
| `http.host`             | `Host` header, falling back to `SERVER_NAME`                                                                                                |
| `http.method`           | HTTP method                                                                                                                                 |
| `http.path`             | Request path, without the query string                                                                                                      |
| `http.query`            | Raw query string                                                                                                                            |
| `http.scheme`           | `http` or `https`                                                                                                                           |
| `http.started_at`       | When the request reached the middleware                                                                                                     |
| `http.status_code`      | Response status: `401`, `403` or `302`                                                                                                      |
| `http.port`             | Request port. Left out if it can't be parsed as an integer                                                                                  |
| `auth.identity`         | The user's `external_user_id` (UUID from the auth service), or their internal ID if they don't have one. Internal env, logged-in users only |
| `auth.identity_type`    | Always `user`. Internal env, logged-in users only                                                                                           |
| `data.event_type`       | Same as `event`                                                                                                                             |
| `data.user_agent`       | `User-Agent` header, or an empty string                                                                                                     |
| `data.internal_user_id` | CMS user ID. Internal env, logged-in users only                                                                                             |
| `data.email`            | CMS user email. Internal env, logged-in users only                                                                                          |
| `data.ip_address`       | Client IP (`REMOTE_ADDR`, as rewritten by `django-xff`). Internal env only, including for anonymous users                                   |

## Audit logging

Wagtail records content activity as `PageLogEntry` (pages) and `ModelLogEntry` (everything else) rows. These are what you see in the admin's history views and site history report. [`cms/core/audit.py`](https://github.com/ONSdigital/dis-wagtail/tree/main/cms/core/audit.py) adds a `post_save` receiver to both models. When a new entry is created, it is also logged to the `cms.audit` logger at `INFO` level. Updates to existing entries are not logged. The module is imported in `CoreConfig.ready()`.

The receiver is registered in every environment. In practice entries only come from the internal environment, because the external one has no admin and can't write to these tables.

Because the receiver listens on the log entry models, anything that writes to Wagtail's audit log is mirrored automatically. That includes Wagtail's built-in actions (`wagtail.create`, `wagtail.edit`, `wagtail.publish`, `wagtail.unpublish`, `wagtail.delete`, `wagtail.revert`, workflow actions and so on) and the project's custom actions below. To audit something new, register an action with the [`register_log_actions`](https://docs.wagtail.org/en/stable/reference/hooks.html#register-log-actions) hook and call `wagtail.log_actions.log()`. It reaches stdout with no extra work.

### Custom actions

| Action                                                                                                                                               | Logged when                                                                                          | Cooldown |
| ---------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | -------- |
| `pages.edit_view`                                                                                                                                    | A user opens the page editor (GET only)                                                              | Yes      |
| `snippets.edit_view`                                                                                                                                 | A user opens a snippet editor (GET only)                                                             | Yes      |
| `pages.preview_mode_used`                                                                                                                            | A user previews a saved page. `data.preview_mode` records the mode                                   | Yes      |
| `wagtail.edit` (pages)                                                                                                                               | A page revision is saved, including autosave. This is a built-in action, rate limited by the project | Yes      |
| `content.chart_download`                                                                                                                             | A chart CSV is downloaded from a page revision                                                       | No       |
| `content.table_download`                                                                                                                             | A table CSV is downloaded from a page revision                                                       | No       |
| `topics.headline_figures_chooser.view`                                                                                                               | The headline figures chooser is opened for a topic page                                              | No       |
| `bundles.edit_view`                                                                                                                                  | A user opens the bundle editor                                                                       | Yes      |
| `bundles.inspect`                                                                                                                                    | A user opens the bundle inspect view                                                                 | Yes      |
| `bundles.preview`                                                                                                                                    | A page or release calendar entry in a bundle is previewed                                            | No       |
| `bundles.preview.attempt`                                                                                                                            | A user tries to preview bundle content without permission                                            | No       |
| `bundles.update_status`, `bundles.approve`, `bundles.teams_changed`, `bundles.pages_changed`, `bundles.datasets_changed`, `bundles.schedule_changed` | Bundle changes (see [Bundles](custom-features/bundles.md#logging))                                   | No       |

Formatters for the core actions are in `register_core_log_actions` in [`cms/core/wagtail_hooks.py`](https://github.com/ONSdigital/dis-wagtail/tree/main/cms/core/wagtail_hooks.py). Bundle actions are in [`cms/bundles/wagtail_hooks.py`](https://github.com/ONSdigital/dis-wagtail/tree/main/cms/bundles/wagtail_hooks.py) and topic actions in [`cms/topics/wagtail_hooks.py`](https://github.com/ONSdigital/dis-wagtail/tree/main/cms/topics/wagtail_hooks.py).

### Example

```json
{
    "created_at": "2026-11-13T10:15:30.123Z",
    "namespace": "cms.audit",
    "severity": 3,
    "event": "Audit event: pages.preview_mode_used",
    "data": {
        "event": "pages.preview_mode_used",
        "object_type": "statistical article page",
        "object_id": 123,
        "object_label": "Labour market overview",
        "user_id": 4,
        "email": "editor@example.com",
        "data": { "preview_mode": "default" },
        "timestamp": "2026-11-13T10:15:30.120456+00:00"
    }
}
```

### Fields

See [Common fields](#common-fields) for `created_at`, `namespace`, `severity` and `event`.

| Field               | Description                                                                                                                                      |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `data.event`        | The action, e.g. `wagtail.publish` or `pages.edit_view`. Filter on this rather than the top-level `event`, which has an `Audit event: ` prefix   |
| `data.object_type`  | Verbose name of the object's content type, e.g. `statistical article page`, `bundle`, `image`                                                    |
| `data.object_id`    | Primary key of the object. An integer for pages and a string for other models, because Wagtail stores `ModelLogEntry.object_id` as text          |
| `data.object_label` | The object's title or string representation at the time of the action, so it stays readable after the object is renamed or deleted               |
| `data.user_id`      | CMS user ID. `null` for system actions such as scheduled publishing                                                                              |
| `data.email`        | CMS user email. `null` for system actions                                                                                                        |
| `data.data`         | Action-specific details, e.g. `preview_mode`, `chart_id` and `revision_id`, or old and new values for bundle changes. `{}` if there are none     |
| `data.timestamp`    | When Wagtail created the log entry, with microsecond precision. Use this for the order of actions; `created_at` is when the log line was written |

### Cooldown

Some actions happen far more often than anyone needs to see them. An editor with the page editor open can autosave many times a minute, each preview refresh is a new preview request, and moving between tabs reloads the edit view. Logging every one would bury the real activity under thousands of identical "viewed" and "edited" entries, increase SIEM ingestion costs, and fill up the history tab in the admin. The cooldown keeps one entry per user and object per time window. You can still see who looked at or edited what, and roughly when, without the duplicates.

The window is set by `CMS_AUDIT_LOG_COOLDOWN_SECONDS` in `cms/settings/base.py` (env var of the same name, default `30`). Views and models read it through an `audit_log_cooldown_seconds` attribute, so tests can override it per class.

The logic is the same everywhere:

1. Build a cache key from the action, the object and the user.
2. If the key is in the cache, skip the log entry.
3. Otherwise, write the log entry and set the key, with a timeout of `CMS_AUDIT_LOG_COOLDOWN_SECONDS`.

Suppressed requests don't refresh the key, so the window is fixed rather than sliding. An editor who keeps refreshing the edit view gets one entry every 30 seconds, not one in total.

| Action                    | Cache key                                                   | Where                                  |
| ------------------------- | ----------------------------------------------------------- | -------------------------------------- |
| `pages.edit_view`         | `page_edit_view_log:{page_id}:{user_id}`                    | `before_edit_page` hook                |
| `snippets.edit_view`      | `snippet_edit_view_log:{model_label}:{object_id}:{user_id}` | `before_edit_snippet` hook             |
| `pages.preview_mode_used` | `preview_page_log:{page_id}:{user_id}:{mode}`               | `BasePage._log_preview()`              |
| `wagtail.edit` (pages)    | `page_edit_save_log:{page_id}:{user_id or "none"}`          | `BasePage.save_revision()`             |
| `bundles.edit_view`       | `bundle_edit_log:{bundle_id}:{user_id}`                     | `BundleEditView._log_bundle_view()`    |
| `bundles.inspect`         | `bundle_inspect_log:{bundle_id}:{user_id}`                  | `BundleInspectView._log_bundle_view()` |

Each user, object and (for previews) preview mode has its own window. Two editors on the same page are both logged, and switching to a different preview mode is logged straight away.

#### Things to be aware of

- **Page and snippet edit views only log GET requests.** Saving the form is a POST and is covered by `wagtail.edit`. The bundle edit and inspect views log every request that reaches `dispatch`.
- **Only page saves are rate limited.** `BasePage.save_revision()` turns `log_action` off when the key is already set, so the revision is still saved but no `wagtail.edit` entry is written. Other actions during the save, such as publishing, are always logged.
- **Reverts are never suppressed.** When `previous_revision` is passed, Wagtail logs `wagtail.revert`. That's a deliberate action, so the cooldown is skipped.
- **Previews of unsaved pages aren't logged**, because there's no primary key to log against.
- **The cooldown uses the default cache.** In deployed and dev environments that's Redis, so the window is shared between processes and containers. Redis errors are ignored (`IGNORE_EXCEPTIONS`), so if Redis is down every lookup misses and every event is logged. The cooldown fails open: you get more entries, never fewer.
- **The cooldown is off in tests.** The test settings use `DummyCache`, so tests that check cooldown behaviour override `CACHES` (see `cms/core/tests/test_edit_save_audit.py`).

Only use the cooldown for high-volume, low-risk "viewed" or autosave-style events. Anything that changes state or might indicate misuse, such as downloads, denied previews or publishing, should be logged every time.

### Known gaps

Wagtail's multiple-upload views for images and documents (the default upload UI) don't create `wagtail.create` entries, so those uploads aren't audited. Tests in `cms/images/tests/test_audit.py` and `cms/documents/tests/test_audit.py` document this.

## Common fields

`cms.core.logs.JSONFormatter` produces every JSON log line, including both systems above:

| Field        | Description                                                                                                              |
| ------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `created_at` | When the log line was written, in UTC with millisecond precision                                                         |
| `namespace`  | Logger name: `cms.security` for protective monitoring, `cms.audit` for audit events                                      |
| `severity`   | ONS severity: `0` critical, `1` error, `2` warning, `3` info. Protective monitoring events are `2`, audit events are `3` |
| `event`      | Log message                                                                                                              |
| `data`       | Everything passed in `extra`, apart from `auth`, `classification` and `http`, which the formatter moves to the top level |

JSON output is on when stdout isn't a TTY, i.e. in containers. Set `LOG_AS_JSON=true` to force it locally. Otherwise you get the plain `verbose` format, which doesn't include the `extra` fields.

## Related events

These aren't part of either system but are useful alongside them. All are internal env only.

Login events come from Django's auth signals in [`cms/users/signal_handlers.py`](https://github.com/ONSdigital/dis-wagtail/tree/main/cms/users/signal_handlers.py) and are logged to `cms.users`. They aren't stored in the database.

| `data.event`        | Level     | Fields                                         |
| ------------------- | --------- | ---------------------------------------------- |
| `user_logged_in`    | `INFO`    | `user_id`, `email`, `ip_address`, `user_agent` |
| `user_logged_out`   | `INFO`    | `user_id`, `email`, `ip_address`, `user_agent` |
| `user_login_failed` | `WARNING` | `username`, `ip_address`, `user_agent`         |

Opening the dataset chooser for unpublished datasets logs `Unpublished datasets requested` with the username to `cms.datasets.views`. This is the default, and always the case when choosing datasets for a bundle. See `DatasetBaseChooseViewMixin` in [`cms/datasets/views.py`](https://github.com/ONSdigital/dis-wagtail/tree/main/cms/datasets/views.py).

## Configuration

| Setting                          | Default | Description                                                                                |
| -------------------------------- | ------- | ------------------------------------------------------------------------------------------ |
| `IS_EXTERNAL_ENV`                | `false` | Switches to the external environment, which disables protective monitoring and PII in logs |
| `CMS_AUDIT_LOG_COOLDOWN_SECONDS` | `30`    | Cooldown window for rate-limited audit actions, in seconds                                 |
| `LOG_AS_JSON`                    | auto    | Output JSON logs. Defaults to `true` when stdout isn't a TTY                               |

## Code and tests

- [`cms/core/middleware.py`](https://github.com/ONSdigital/dis-wagtail/tree/main/cms/core/middleware.py): `UnauthorizedAccessLoggingMiddleware`
- [`cms/core/audit.py`](https://github.com/ONSdigital/dis-wagtail/tree/main/cms/core/audit.py): audit log mirroring
- [`cms/core/models/base.py`](https://github.com/ONSdigital/dis-wagtail/tree/main/cms/core/models/base.py): preview logging and page save cooldown
- [`cms/core/logs.py`](https://github.com/ONSdigital/dis-wagtail/tree/main/cms/core/logs.py): JSON formatter
- [`cms/settings/base.py`](https://github.com/ONSdigital/dis-wagtail/tree/main/cms/settings/base.py): `CMS_AUDIT_LOG_COOLDOWN_SECONDS`, middleware and logger config

Tests:

- `cms/core/tests/test_middleware.py`: denied access logging, including `IS_EXTERNAL_ENV=True`
- `cms/core/tests/test_audit.py`: mirroring to stdout
- `cms/core/tests/test_edit_save_audit.py`: page save cooldown
- `cms/core/tests/test_preview_audit.py`: preview logging and cooldown
- `cms/core/tests/test_wagtail_hooks.py`: page and snippet edit view logging
- `cms/users/tests/test_signal_handlers.py`: login events
- `cms/images/tests/test_audit.py`, `cms/documents/tests/test_audit.py`: media audit entries

## Related documentation

- [Wagtail audit log](https://docs.wagtail.org/en/stable/extending/audit_log.html)
- [`register_log_actions` hook](https://docs.wagtail.org/en/stable/reference/hooks.html#register-log-actions)
- [Logging](integrations/logging.md)
- [Deployment environments](custom-features/deployment-environments.md)
- [Bundles](custom-features/bundles.md)
