# RetiBoard Privacy Guide

This page explains how to use RetiBoard privately in practical terms.

RetiBoard is designed so that:

- post content stays encrypted in transit and at rest
- decryption happens locally in your browser
- routing nodes forward traffic but do not need your decryption keys
- moderation and filtering stay local to your own client

That is a strong privacy baseline, but privacy still depends on how you connect to the network and how you operate your node.

## The Short Version

If you want to use RetiBoard privately:

1. Use a privacy-preserving first hop to Reticulum.
2. Treat identities as disposable personas, not permanent accounts.
3. Avoid unnecessary pattern leakage in how and when you post.
4. Use a clean browser and a trustworthy local machine.

If you follow those four rules, RetiBoard offers strong practical privacy for its intended use.

## What RetiBoard Protects Well

RetiBoard is built so that your actual post content is not exposed to infrastructure.

- Content is encrypted before it is stored or shared.
- Decryption happens locally in the browser, not on the backend.
- The backend stores opaque payload blobs and does not inspect them.
- Reticulum identities do not reveal your real-world identity by themselves.

In normal use, this means other people may see that traffic exists, but they should not be able to read your post content unless they already have access to the board.

## Your First-Hop Connection Matters Most

The most important privacy point is your first connection into the Reticulum network.

If you connect to a TCP node directly, that node can see your IP address. This is normal for TCP networking and is not unique to RetiBoard. After that first hop, routing operates on Reticulum identities and destinations rather than your browser fingerprint or a web account identity.

### Best Practice

- Use Tor, a VPN, or another privacy-preserving tunnel for your first-hop TCP connection.
- Prefer a setup you control or trust.
- Do not assume a public first-hop node is blind to your network origin.

### Practical Guidance

- For casual use, a reputable VPN is already a meaningful improvement.
- For stronger network privacy, use Tor or a carefully designed tunnel setup.
- If privacy matters for a specific session, decide your first-hop path before launching RetiBoard.

The simplest honest summary is this: if your first hop is private, RetiBoard becomes much more private in practice.

## Identity Privacy

RetiBoard uses Reticulum identities so conversations, replies, and filtering can work without accounts. This is a practical privacy model, not a social-media identity model.

An identity in RetiBoard should be treated as a persona under your control.

- You can keep one identity if you want continuity.
- You can rotate identities whenever you want.
- You can use separate identities for separate contexts.

### Best Practice

- Do not reuse the same identity everywhere unless you want continuity.
- Separate identities by board, topic, or risk level when needed.
- Rotate identities if an identity becomes too recognizable.

### Important Mindset

Privacy here comes from user control. A persistent identity is useful only because it is optional, local, and disposable. If you want less linkability, rotate more often and compartmentalize.

## Metadata Correlation

RetiBoard protects content better than it hides patterns. Like any distributed system, some structural metadata exists so the network can function.

Examples include:

- when you were active
- which board or thread you interacted with
- reply timing and posting cadence
- whether a post had attachments

This does not reveal your plaintext content, but repeated behavior can still form patterns over time.

### Best Practice

- Avoid posting in rigid routines.
- Avoid reusing one identity across unrelated boards or subjects.
- Do not post highly distinctive personal details or habits.
- If privacy matters, reduce attachment usage and unnecessary activity bursts.

### Simple Rule

Think of content privacy and pattern privacy as separate things. RetiBoard gives you strong content privacy. Pattern privacy improves when you vary timing, compartmentalize identities, and keep your first hop private.

## Browser and Device Hygiene

Because decryption happens locally, your browser and local machine are part of the trust boundary.

If your browser is hostile, your extensions are malicious, or your computer is compromised, no application-level encryption can fully protect you at the moment of decryption.

### Best Practice

- Use a clean browser profile for RetiBoard.
- Avoid unnecessary extensions.
- Keep your browser and operating system updated.
- Do not run RetiBoard on a machine you do not trust.
- Lock down local access to your device and user account.

### Practical Setup

A dedicated browser profile is usually enough for most users. If your threat model is higher, use a dedicated machine or compartment for RetiBoard activity.

## Board Privacy

Board content is encrypted, but a board is not a secret society by cryptographic membership control alone.

In practice:

- board access depends on who has the board announce and how widely it spreads
- sharing a board widely makes it more public
- sharing a board carefully keeps it more private in practice

### Best Practice

- Share sensitive boards intentionally, not casually.
- Treat board discovery links and announce data as access material.
- Use invite-style distribution for semi-private boards.

This is not a flaw in normal use. It simply means privacy depends partly on how broadly board access is shared.

## Recommended Privacy Habits

- Use a private first hop.
- Keep identities compartmentalized.
- Use a clean browser profile.
- Avoid posting unique personal details.
- Be mindful of timing patterns if you need stronger privacy.
- Share boards deliberately.

## Bottom Line

RetiBoard is private when used correctly.

Its strongest privacy properties are local decryption, encrypted payloads, and infrastructure neutrality. The main things users must manage for themselves are first-hop network privacy, identity reuse, metadata habits, and local browser/device trust.

Used with those basics in mind, RetiBoard provides strong practical privacy without requiring centralized accounts or trust in a content-hosting server.
