# Security policy

## Supported versions

Pre-1.0, only the latest minor version receives security fixes. After 1.0, the
current major version and the previous minor.

| Version | Supported |
|---|---|
| 0.1.x | ✅ |

## Reporting a vulnerability

**Do not open a public issue.**

Use [GitHub's private vulnerability
reporting](https://github.com/IncepTools/python/security/advisories/new), or
email the maintainers listed in [MAINTAINERS.md](MAINTAINERS.md).

Please include:

- what an attacker can do, and what they need in order to do it;
- the smallest reproduction you can manage;
- the version you tested.

You will get an acknowledgement within three working days and an assessment
within ten. If we disagree that something is a vulnerability we will say so and
explain why, rather than going quiet.

## What counts

This framework's job is to make certain failures impossible, so a hole in one
of those guarantees is a vulnerability even if it needs an unusual
configuration:

- **Tenant isolation.** Any way to read or write another account's rows through
  a scoped repository, including by sending an account id in a payload.
- **Information disclosure through the envelope.** A driver message, a
  constraint name, a hostname, a stack trace or a secret reaching a client.
- **Authorization bypass.** A mutating route reachable without its guards, a
  guard that can be made to pass, or `@Public` applying where it was not
  declared.
- **Signature verification.** A webhook signature accepted when it should not
  be, or a timing side channel in the comparison.
- **Idempotency.** A replayed request running twice, or one caller replaying
  another's response.
- **Locking.** A distributed lock releasing a holder it does not own, allowing
  two replicas to run the same job.

## What does not

- A denial of service from a configuration the documentation already warns
  about — for example the in-memory rate-limit store with many replicas.
- Depending on a package with a CVE that this framework's own code paths cannot
  reach. Report it as a normal issue and we will bump it.
- Anything requiring the attacker to already execute code in the process.

## Disclosure

We aim to ship a fix and publish an advisory together, crediting the reporter
unless they prefer otherwise. If a fix will take longer than 90 days we will
tell you why and agree a date.
