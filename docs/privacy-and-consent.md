# Privacy & consent

The SDK is built to make GDPR/CCPA compliance straightforward: it can buffer everything locally and
send nothing until the user agrees, and it honors withdrawal by deleting what it holds.

- [The consent model](#the-consent-model)
- [Enabling consent gating](#enabling-consent-gating)
- [Recording the user's choice](#recording-the-users-choice)
- [Withdrawal](#withdrawal)
- [Changing the requirement across app versions](#changing-the-requirement-across-app-versions)
- [What the SDK collects](#what-the-sdk-collects)

---

## The consent model

Consent gating is **off by default** — the SDK collects and uploads immediately, which is the right
choice for apps that don't operate under an opt-in regime. Turn it on and the SDK will *collect and
buffer to disk* but *upload nothing* until you record a decision.

Internally each install is always in one of four states, persisted across launches:

| State | Meaning | Uploading? |
|---|---|---|
| **NotRequired** | Consent gating is off. | Yes |
| **Pending** | Gating is on; the user has not decided yet. | No — buffered locally |
| **Granted** | The user agreed. | Yes |
| **Denied** | The user declined (or withdrew). | No — and buffered data is discarded |

You never set these directly; you enable the requirement (config) and record the user's yes/no
([`set_user_consent`](api-reference.md#set_user_consent)).

## Enabling consent gating

Set `require_user_consent=True` in the config you pass to `initialize()`:

```python
keewano_sdk.initialize(KeewanoConfig(api_key="…", require_user_consent=True))
```

New installs then start in **Pending**: data accumulates on disk (subject to the 50 MB cap) but
nothing leaves the device until you record a choice.

## Recording the user's choice

Wire your consent prompt's result straight to:

```python
keewano_sdk.set_user_consent(True)    # Granted — flush the buffer and keep collecting
keewano_sdk.set_user_consent(False)   # Denied  — discard the buffer and stop
```

The decision is persisted, so you only need to ask once; on later launches the SDK resumes in the
recorded state. (The sample app shows one way to ask on the command line — see
[`sample/main.py`](../sample/main.py).)

## Withdrawal

**Withdrawal is honored from any state, including after a prior `True`.** Calling
`set_user_consent(False)`:

- deletes the event batches still on disk,
- discards what is buffered in memory, and
- stops uploading.

This is the mechanism behind GDPR Art. 7(3) ("it shall be as easy to withdraw as to give consent").
Wire it directly to your opt-out toggle — it is not a no-op for users who previously agreed.

## Changing the requirement across app versions

If you ship with `require_user_consent=False` and later flip it to `True`, installs that were **never
asked** are moved from *NotRequired* to *Pending* on their next launch, rather than keeping the
permissive setting they were created under. A decision the user **actually made** (Granted or Denied)
always outranks a config change and is preserved in both directions.

## What the SDK collects

The SDK collects behavioral and technical analytics — see [Automatic tracking](automatic-tracking.md)
for the small automatic set (a launch burst and uncaught exceptions) and the
[API reference](api-reference.md) for what you report explicitly. Identity is **anonymous by
default**: a random per-install ID ([`get_install_id`](api-reference.md#get_install_id)) with no
device advertising ID or PII. You may associate your own user id with an install via
[`set_user_id`](api-reference.md#set_user_id); what that id means, and its lawful basis, is yours to
define.
