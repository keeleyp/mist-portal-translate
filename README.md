# Mist Portal Translate

Fills in every language of a Mist WLAN guest portal from its default
(English) text, so admins don't have to translate each prompt by hand in
the portal editor.

## How it works

1. Reads the WLAN and downloads its portal template (via the WLAN's signed `portal_template_url`).
2. For each language set to `Y` in `[languages]`, matches each default text field against `translations/<locale>.json` (English text → translation) and fills that language in. `en-GB` / `en-US` copy the default text.
3. Backs up the current template to `backups/`, writes it with `PUT /orgs/{org_id}/wlans/{wlan_id}/portal_template`, then re-reads it to confirm every value was saved.

Languages set to `N` are left untouched.

## Setup

```bash
pip install -r requirements.txt
cp mist_portal_translate.ini.example mist_portal_translate.ini
```

Fill in `org_id`, `api_token` (it needs write access), `cloud` and `wlan_id`. Then set each language to Y or N.

```bash
python3 mist_portal_translate.py
```

Set `dry_run = Y` first if you want to review the result without writing to Mist. The proposed template is saved to `backups/..._PREVIEW.json`.

## Options

| Option | Default | Meaning |
|---|---|---|
| `dry_run` | N | Build and preview only; nothing is written to Mist |
| `overwrite_existing` | Y | Replace text already set for a language. N fills only empty fields |
| `translate_policy_text` | N | Also translate the Terms of Service, Privacy and Marketing body text. Off by default because this is your own legal wording |

## Limitations

- The translations are fixed, not live machine translation. They cover Mist's stock English wording. If you've customised a default field (e.g. a new welcome message), that field is reported as "untranslated" and falls back to the default language. Add your text and its translations to the `translations/*.json` files to cover it.
- The translations were written by an AI (Claude). Have a native speaker review any language before using it in production.

## Proxy / Zscaler

The optional `[network]` section in the .ini supports a custom CA bundle (`ca_bundle`), disabling TLS verification as a last resort (`verify_ssl`), and explicit proxies. Leave it blank for normal behaviour.
