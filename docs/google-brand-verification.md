# Google brand verification for the Manifold OAuth client

Background task from the Phase 3 gate 4 amendment (DECISIONS.md, 2026-09-13). Needed before
the Google OAuth client can leave Testing status, which is needed before a Drive toolset
can hold a refresh token for more than seven days. Sheets does not need this; it runs on
a service account.

Brand verification is a light review of the consent screen's identity: name, links,
domain ownership. It is not scope verification. Sensitive scopes stay unverified, so the
"Google hasn't verified this app" screen and the 100-user cap remain, which is fine for
one user. Google says the automated review "typically takes a few minutes" and a manual
one "2-3 business days".

## The three pages

All three must be on `mannylab.cloud`, the authorised domain on the consent screen, and
publicly reachable with no Cloudflare Access in front. A static site on a subdomain such
as `manifold.mannylab.cloud`, or plain pages on `mannylab.cloud` itself, both work. They
must not sit under `mcp.mannylab.cloud`, which is behind Access.

### 1. Home page, required

Google: "Your home page must be publicly accessible, and not just accessible to your
site's logged-in users." It must "include a description of the app's functionality, as
well as links to the privacy policy and optional terms of service."

Content that satisfies this:

- The app name exactly as on the consent screen: Manifold.
- One paragraph on what it does: a self-hosted gateway that lets the owner's own AI
  assistant read and edit the owner's own Google Sheets and Drive files, on the owner's
  own server.
- Who it is for: a single user, the owner. Not offered to the public.
- Links to the privacy policy and the terms page.
- A contact email.

### 2. Privacy policy, required

Google: "The privacy policy must be visible to users, hosted within the same domain as
your application's home page, and linked to on the OAuth consent screen." It "must
disclose the manner in which your application accesses, uses, stores, or shares Google
user data."

Content that satisfies this, in plain sentences:

- What is accessed: Google Sheets and, later, Google Drive files that the signed-in
  user owns or has access to, using the scopes granted on the consent screen.
- Why: to let the user's AI assistant read and edit those files at the user's request.
- Storage: OAuth tokens are stored encrypted on the user's own server. File contents are
  read when a tool is called and are not retained; the audit log stores a hash of tool
  arguments, never the arguments or file contents.
- Sharing: no data is shared with anyone. No advertising, no analytics, no third parties.
- Deletion: the user can revoke access at any time from the credential page in Manifold
  or at https://myaccount.google.com/permissions, and can delete stored tokens by deleting
  the credential.
- A statement that use of Google user data complies with the Google API Services User
  Data Policy, including the Limited Use requirements. Google looks for this sentence.
- Contact email and the date.

### 3. Terms of service, optional but fill the field

Google lists it as optional. Filling it in avoids a later request. Two paragraphs are
enough: the software is provided as is, for the owner's personal use, with no warranty.

## Domain ownership

Google: "Google requires verification of all domains that are associated with an
application's OAuth consent screen and credentials." Verify `mannylab.cloud` in Google
Search Console with the same Google account that owns the Cloud project, using the DNS
TXT record method. Cloudflare DNS makes this a two-minute job.

## Submission steps

1. Google Cloud Console, Google Auth Platform, Branding.
2. Fill Application home page, Application privacy policy link and Application terms of
   service link with the three URLs. Leave the logo empty; a logo is not required and
   adding one later means re-verifying.
3. Confirm `mannylab.cloud` is listed under Authorised domains and verified in Search
   Console.
4. Save. The page now holds a Draft Branding.
5. Click Verify Branding. Wait for the status to show Ready to publish. If it asks for
   changes, the message names the page and the missing item.
6. Click Publish branding.
7. Google Auth Platform, Audience, Publish app. Confirm. Status becomes In production.
8. In Manifold, open the Google credential and click Reconnect once, so the new refresh
   token is issued under Production status.

Nothing in Manifold changes for this. The credential, scopes and redirect URI stay as
they are.
