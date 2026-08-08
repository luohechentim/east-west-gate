# Security Policy

## Supported version

Security fixes are made on the latest released version on the `main` branch.

## Reporting a vulnerability

Please do **not** open a public issue for a suspected vulnerability. Use GitHub's private vulnerability reporting feature from the repository's **Security** tab. If private reporting has not yet been enabled after the repository is created, contact the repository owner privately through their GitHub profile.

Include a minimal reproduction, affected version, impact, and any suggested mitigation. Remove API keys, customer data, and private prompts from the report. Maintainers will acknowledge a report, investigate it, and coordinate disclosure before publishing details.

## Scope notes

Double Gate can send content to user-configured model endpoints. Treat endpoint choice, API keys, submitted content, and persisted audit reports as sensitive operational inputs. The package does not make an offline stub review equivalent to an independent human or multi-provider review.
