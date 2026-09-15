# Configuration reference

The SDK is configured entirely **in code**: build a [`KeewanoConfig`](#keewanoconfig) and pass it to
`keewano_sdk.initialize()`. There is no manifest, no environment auto-detection, and no
auto-initialization — a native Python application has no process-launch hook, so you own the call.

- [KeewanoConfig](#keewanoconfig)
- [Initialization](#initialization)
- [Where data is stored](#where-data-is-stored)
- [Endpoint override](#endpoint-override)
- [Test mode](#test-mode)

---

## KeewanoConfig

Only `api_key` is required; every other field defaults to the safe, fully-featured behavior, so a
minimal integration passes one argument.

```python
from keewano_sdk import KeewanoConfig

@dataclass
class KeewanoConfig:
    api_key: str                                   # required
    require_user_consent: bool = False
    app_version: str = ""
    data_dir: Optional[str] = None
    disable_exception_tracking: bool = False
    endpoint: str = DEFAULT_ENDPOINT               # should not be set in production
    proxy_auth_bearer: Optional[str] = None        # should not be set in production
    custom_event_set: Optional[CustomEventSet] = None
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `api_key` | `str` | — (required) | Your Keewano project API key (a JWT). Blank or `None` ⇒ the SDK stays inert. |
| `require_user_consent` | `bool` | `False` | Buffer locally and send nothing until [consent](privacy-and-consent.md) is recorded. |
| `app_version` | `str` | `""` | Your application's version string, reported with the launch burst. There is no manifest to read it from, so supply it here. |
| `data_dir` | `Optional[str]` | `None` | Directory for durable identifiers, consent, and pending batches. `None` ⇒ a per-user application-data directory (see [Where data is stored](#where-data-is-stored)). |
| `disable_exception_tracking` | `bool` | `False` | Do **not** install the hook that auto-reports uncaught exceptions. Not recommended — crash context is valuable. See [Automatic tracking](automatic-tracking.md). |
| `endpoint` | `str` | `DEFAULT_ENDPOINT` | The Keewano ingress base URL. Override only for integration testing — see [Endpoint override](#endpoint-override). |
| `proxy_auth_bearer` | `Optional[str]` | `None` | Bearer token sent as `Authorization: Bearer <value>` on every request, for a test ingress behind an authenticating proxy. Leave `None` in production. |
| `custom_event_set` | `Optional[CustomEventSet]` | `None` | The codegen-produced custom-event definitions. `None` ⇒ no custom events. See [Custom events](custom-events.md). |

## Initialization

```python
keewano_sdk.initialize(config: KeewanoConfig) -> None
```

Call it **once at startup**. It is idempotent (subsequent calls are ignored), safe to call from any
thread, and **never raises** — a failure inside it disables the SDK and logs, rather than propagating
into your app. A blank `api_key` leaves the SDK inert.

```python
import keewano_sdk
from keewano_sdk import KeewanoConfig

keewano_sdk.initialize(KeewanoConfig(
    api_key="YOUR_KEEWANO_API_KEY",
    app_version="1.0.0",
    require_user_consent=False,
))
```

Both call styles are equivalent throughout the SDK — module-level functions or the `KeewanoSDK`
facade:

```python
keewano_sdk.initialize(config)
# is the same callable as
from keewano_sdk import KeewanoSDK
KeewanoSDK.initialize(config)
```

## Where data is stored

Durable identifiers, the consent decision, and pending batches live in a per-user application-data
directory, chosen with the standard library and to each such prefix a unique (based on the API key) sub directory is added:

| OS | Default location |
|---|---|
| Linux | `$XDG_DATA_HOME/keewano` (else `~/.local/share/keewano`) |
| macOS | `~/Library/Application Support/Keewano` |
| Windows | `%LOCALAPPDATA%\Keewano` |

Override it by setting `data_dir`:

Note: if running multiple processes on the same file system with the same API key then there will be a colision on the
data directory. If this is the case, override the directory setting so that each process has its own directory.


```python
keewano_sdk.initialize(KeewanoConfig(api_key="…", data_dir="/var/lib/myapp/keewano"))
```

Choose a directory your process can write to and that persists across runs — it holds the anonymous
install identity and any events buffered while offline.

## Endpoint override

`endpoint` points the SDK at a different ingress. It is intended for **integration testing against a
staging ingress only** — leave it unset in production to use the default
(`DEFAULT_ENDPOINT`). The value is treated as a **base URL**: the SDK appends `/in` for event uploads
and `/custom` for custom-event registration.

Use an **HTTPS** endpoint: the API key travels as a request header, so a plaintext endpoint would put
a credential on the wire. `proxy_auth_bearer` exists for the common test setup where that staging
ingress sits behind an authenticating proxy.

## Test mode

To keep your own testing out of production analytics, tag this install as a test user:

```python
keewano_sdk.mark_as_test_user("qa-alice")
```

Keewano's AI then excludes this install from production metrics. See
[`mark_as_test_user`](api-reference.md#mark_as_test_user) in the API reference.
