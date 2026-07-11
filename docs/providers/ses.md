# Amazon SES connection

[Amazon SES](https://aws.amazon.com/ses/) is AWS's transactional email
service. Mailtivo-Relay talks to the **SES v2 API** directly (SigV4-signed
HTTPS, no boto3) and receives events through **SNS** — SES has no native
webhooks, so events flow SES → configuration set → SNS topic → HTTPS
subscription pointing at the relay.

## What you need from AWS

1. **A verified identity** — the domain (recommended) or address you will
   send from, verified in SES **in the region you'll use**. For production
   your account must be out of the SES sandbox (request production access in
   the SES console), otherwise SES only delivers to verified addresses.
2. **IAM access keys** — an access key id + secret access key for an IAM
   user. Minimal policy: `ses:SendEmail` (sending) and `ses:GetAccount`
   (the relay's *Test connection* health check).
   > Use long-lived IAM user keys. Temporary STS credentials (session
   > tokens) are **not** supported — the relay does not send
   > `X-Amz-Security-Token`.
3. **AWS region** — e.g. `us-east-1`. The relay derives the endpoint from it
   (`email.<region>.amazonaws.com`); the Base URL field is ignored for SES.
4. **A configuration set + SNS topic** (below) — without a configuration
   set, mail sends fine but **no delivery/bounce/complaint events ever
   reach the relay**.

## Configure in Mailtivo-Relay

1. **Connections** → *Add connection*.
2. Pick **Amazon SES** as the provider.
3. Fill in:
   - Name: anything you like (e.g. `ses-prod`).
   - AWS region: as above.
   - Access key ID / Secret access key: as above (stored encrypted).
   - Configuration set: the name you create in the next section.
4. Click *Test* — the relay calls SES `GetAccount`; a 200 means the region
   and credentials are valid.

## Configure SES events (SNS)

In the AWS console, in the **same region** as the connection:

1. **SNS** → *Topics* → create a **Standard** topic (e.g.
   `mailtivo-relay-events`).
2. **SES** → *Configuration sets* → create one (e.g. `mailtivo-relay`) →
   *Event destinations* → add an **Amazon SNS** destination pointing at the
   topic. Select at minimum **Sends, Deliveries, Hard bounces, Complaints,
   Rejects, Delivery delays**. Add **Opens / Clicks** if you want engagement
   events in the timeline (this enables SES open/click tracking).
3. Enter the configuration set name on the relay Connection (step 3 above) —
   the relay only attaches `ConfigurationSetName` to outgoing sends when it
   is set, and SES only publishes events for sends made through the set.
4. Back in **SNS** → your topic → *Create subscription*:
   - Protocol: **HTTPS**
   - Endpoint: `https://your-relay/webhooks/ses/<connection_id>/` (the
     connection id is in the URL of the connection's edit page).
   - Leave **raw message delivery OFF** (the default). The relay needs the
     full SNS JSON envelope to verify the signature; raw delivery strips it
     and every event will be rejected with 401.
5. SNS immediately sends a `SubscriptionConfirmation` to the endpoint. The
   relay verifies its SNS signature and confirms it automatically — the
   subscription should show **Confirmed** within seconds. No manual step.

Every SNS message (including the confirmation) is verified against AWS's
X.509 signing certificate before it is trusted; certificate and subscribe
URLs are only ever fetched from `sns.<region>.amazonaws.com` hosts.

**Topic pinning:** the first confirmed subscription locks the connection to
that topic's ARN; events from any other SNS topic are rejected with a 403
afterwards (a valid SNS signature alone doesn't prove the message came from
*your* topic). You can also pre-fill the expected topic ARN on the
connection's edit page before subscribing — recommended, since it closes the
window entirely. If you ever move to a new topic, clear or update the field.

## What the relay does with SES

- Translates outgoing sends to SES v2 `POST /v2/email/outbound-emails`
  (`SendEmail` with `Content.Simple`).
- Maps SES event types:

| SES event | Internal type | Affects message status |
| --- | --- | --- |
| `Send` | `sent` | yes (→ `sent`) |
| `Delivery` | `delivered` | yes |
| `Bounce` | `bounced` | yes |
| `Complaint` | `complained` | yes |
| `Reject` | `failed` | yes |
| `DeliveryDelay` | `deferred` | no |
| `Open` | `opened` | no |
| `Click` | `clicked` | no |
| `Rendering Failure` | `failed` | yes |

## Known gotchas

- **The From address must be a verified SES identity** in the connection's
  region, or SES rejects the send (a permanent failure — the pool router
  will not retry it on another member).
- **New AWS accounts start in the SES sandbox** with a 200/day quota and
  verified-recipients-only delivery. Request production access before
  going live.
- **No configuration set = no events.** Sends succeed but the timeline
  stays at `sent` forever. If events stop arriving, check the subscription
  is still *Confirmed* and raw message delivery is off.
- Custom headers are passed through `Content.Simple.Headers` best-effort;
  byte-exact header fidelity would require raw MIME, which the relay does
  not use. SES `SendEmail` has no scheduling or idempotency-key support.
- DKIM/SPF are owned by **SES** (Easy DKIM / custom MAIL FROM domain),
  not the relay. Configure them on the SES identity.
- Bounce handling: `Permanent` and `Undetermined` bounces auto-suppress the
  recipient; `Transient` (soft) bounces — mailbox full, out-of-office
  auto-replies — are recorded as events but do **not** suppress.

## Reference

Retrieved 2026-06-05 — re-verify before significant changes:

- SendEmail (v2): https://docs.aws.amazon.com/ses/latest/APIReference-V2/API_SendEmail.html
- GetAccount (v2): https://docs.aws.amazon.com/ses/latest/APIReference-V2/API_GetAccount.html
- SigV4 signing: https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv4-signing.html
- Event publishing to SNS: https://docs.aws.amazon.com/ses/latest/dg/notification-contents.html
- SNS signature verification: https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
- Adapter source: [apps/connections/adapters/ses.py](../../apps/connections/adapters/ses.py)
