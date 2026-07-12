# KUKLABS_IDENTITY.md — Mandatory Kuklabs Standard

> **Canonical source of truth:** `amithkukllod777/kukbook-erp/KUKLABS_IDENTITY.md`
>
> This repository follows the canonical Kuklabs identity, authentication, UI, profile and release standard. Read the canonical file completely before architecture, authentication or UI work. Do not create an independent policy variant in this repository.

## Non-negotiable rules

- One Kuklabs Account, central AuthKit and one ecosystem user ID.
- One shared MySQL identity/database infrastructure, except an owner-approved documented exception.
- One shared Google Cloud project and one linked Firebase project; register this app inside them instead of creating new projects.
- Never create a separate users/passwords/OTP/session system or use Firebase/Google UID as the primary Kuklabs user ID.
- Use the shared universal login/signup flow with mobile/email, password, Google, verification, recovery and account linking.
- Across apps use **Inter**, shared typography, neutral/semantic colours, spacing, controls, navigation, profile structure and version/update policy.
- Mobile top bar: 56px. Desktop top bar: 64px. Bottom navigation: 64px plus safe area, with 3–5 labeled items. Desktop sidebar: 256px expanded / 72px collapsed.
- Profile must separate Kuklabs Account identity from product workspace/organisation roles and data.
- Display `Version MAJOR.MINOR.PATCH (Build N)` in Profile → About; force updates only for critical security, compatibility, legal or data-integrity cases.
- Only product icon, product name, tagline, approved accent colour and product-specific features may change.
- Use `Powered by Kuklabs`; do not replace the product app icon with the Kuklabs corporate logo.

## Required implementation check

Before merging product work, verify the canonical file in `kukbook-erp` has been followed. When this local reference and the canonical file differ, the canonical file wins.
