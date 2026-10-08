#!/usr/bin/env python3
"""Translate a Mist WLAN guest portal into other languages and write it back.

The portal template's default-language (English) text fields are matched
against the bundled translations/<locale>.json files, and each locale
flagged Y in the [languages] section of mist_portal_translate.ini is filled
in. The current template is backed up before anything is written, and the
result is re-read from Mist afterwards to confirm it actually persisted.
"""
import configparser
import json
import os
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INI_NAME = "mist_portal_translate.ini"
TRANSLATIONS_DIR = os.path.join(SCRIPT_DIR, "translations")
BACKUP_DIR = os.path.join(SCRIPT_DIR, "backups")

config = configparser.ConfigParser(inline_comment_prefixes=(";",))
config.optionxform = str  # keep locale codes like zh-Hans case-sensitive
config.BOOLEAN_STATES = {**config.BOOLEAN_STATES, "y": True, "n": False}
if not config.read(os.path.join(SCRIPT_DIR, INI_NAME)):
    sys.exit(f"Could not read {INI_NAME} - copy {INI_NAME}.example and fill it in.")

ORG_ID = config.get("mist", "org_id").strip()
API_TOKEN = config.get("mist", "api_token").strip()
CLOUD = config.get("mist", "cloud", fallback="").strip()
WLAN_ID = config.get("mist", "wlan_id").strip()

DRY_RUN = config.getboolean("options", "dry_run", fallback=False)
OVERWRITE_EXISTING = config.getboolean("options", "overwrite_existing", fallback=True)
TRANSLATE_POLICY_TEXT = config.getboolean("options", "translate_policy_text", fallback=False)

# Every locale the Mist portal editor offers. English variants copy the
# default text rather than being translated.
ALL_LOCALES = [
    "ar", "ca-ES", "cs-CZ", "da-DK", "de-DE", "el-GR", "en-GB", "en-US",
    "es-ES", "fi-FI", "fr-FR", "he-IL", "hi-IN", "hr-HR", "hu-HU", "id-ID",
    "it-IT", "ja-JP", "ko-KR", "ms-MY", "nb-NO", "nl-NL", "pl-PL", "pt-BR",
    "pt-PT", "ro-RO", "ru-RU", "sk-SK", "sv-SE", "th-TH", "tr-TR", "uk-UA",
    "vi-VN", "zh-Hans", "zh-Hant",
]
ENGLISH_LOCALES = {"en-GB", "en-US"}

# String fields that are settings rather than visitor-facing text.
NON_TEXT_FIELDS = {
    "color", "alignment", "smsValidityDuration", "smsCountryFormat",
    "sponsorEmailTemplate", "logo",
}
# Placeholders for the customer's own legal text - machine translating real
# legal wording is risky, so these are opt-in via translate_policy_text.
POLICY_TEXT_FIELDS = {"tosText", "privacyPolicyText", "marketingPolicyOptInText"}

REQUEST_TIMEOUT = 60


def build_session():
    """Shared HTTP session, wired up for a TLS-inspecting proxy (e.g. Zscaler)
    when the optional [network] section is present in the .ini. With no
    [network] section, behaviour is unchanged (certifi CA store, and the
    HTTP_PROXY / HTTPS_PROXY / NO_PROXY environment variables if set)."""
    session = requests.Session()
    if "network" not in config:
        return session

    section = config["network"]
    ca_bundle = section.get("ca_bundle", "").strip()
    if ca_bundle:
        ca_path = os.path.expanduser(ca_bundle)
        if not os.path.isfile(ca_path):
            sys.exit(f"[network] ca_bundle does not exist: {ca_path}\n"
                     f"This should be a PEM file containing your proxy's root CA "
                     f"certificate (e.g. exported from Zscaler).")
        session.verify = ca_path
    elif not section.getboolean("verify_ssl", fallback=True):
        session.verify = False
        print("WARNING: TLS certificate verification is DISABLED ([network] verify_ssl = false). "
              "Only use this as a last resort.")
        requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)

    for scheme in ("http", "https"):
        proxy = section.get(f"{scheme}_proxy", "").strip()
        if proxy:
            session.proxies[scheme] = proxy
    return session


SESSION = build_session()


def cloud_to_host(cloud):
    """Normalise the .ini cloud value to an API host URL like https://api.eu.mist.com."""
    value = cloud.lower().strip()
    if "://" in value:
        value = urlparse(value).netloc
    value = value.strip("/")
    if value.endswith("mist.com"):
        if value.startswith("manage."):
            value = "api." + value[len("manage."):]
        return f"https://{value}"
    if value in ("us", "global", "global01"):
        return "https://api.mist.com"
    return f"https://api.{value}.mist.com"


def http(method, url, mist_auth=True, **kwargs):
    """Request with retries on 429 and friendly errors for proxy / Zscaler failures.
    mist_auth=False is for the signed Google Storage URL, which rejects an
    extra Authorization header."""
    headers = {"Authorization": f"Token {API_TOKEN}"} if mist_auth else {}
    for attempt in range(1, 6):
        try:
            resp = SESSION.request(method, url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs)
        except requests.exceptions.SSLError as e:
            sys.exit(f"\nTLS certificate verification failed for {url}: {e}\n"
                     f"If you're behind a TLS-inspecting proxy (e.g. Zscaler), set [network] ca_bundle "
                     f"in {INI_NAME} to the path of its root CA certificate (PEM format).")
        except requests.exceptions.ProxyError as e:
            sys.exit(f"\nCould not reach the proxy for {url}: {e}\n"
                     f"Check [network] http_proxy / https_proxy in {INI_NAME} "
                     f"or your HTTP(S)_PROXY environment variables.")
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 30))
            print(f"  Rate limited - waiting {retry_after}s (attempt {attempt}/5)...")
            time.sleep(retry_after)
            continue
        break
    if resp.status_code == 401:
        sys.exit(f"401 Unauthorized from {url} - the API token is not valid on this cloud ({CLOUD}).")
    if resp.status_code == 403:
        sys.exit(f"403 Forbidden from {url} - the API token can't access org {ORG_ID} (needs write access).")
    if resp.status_code >= 400:
        sys.exit(f"HTTP {resp.status_code} from {method} {url}:\n{resp.text[:1000]}")
    return resp


def fetch_template(api_base):
    """The WLAN only carries a short-lived signed URL to its portal template."""
    wlan = http("GET", f"{api_base}/orgs/{ORG_ID}/wlans/{WLAN_ID}").json()
    url = wlan.get("portal_template_url")
    if not url:
        sys.exit(f"WLAN {WLAN_ID} ({wlan.get('ssid')}) has no portal_template_url - "
                 f"is the guest portal enabled on it?")
    return wlan, http("GET", url, mist_auth=False).json()


def selected_locales():
    if "languages" not in config:
        sys.exit(f"No [languages] section in {INI_NAME}.")
    section = config["languages"]
    unknown = [k for k in section if k not in ALL_LOCALES]
    if unknown:
        sys.exit(f"Unknown locale(s) in [languages]: {', '.join(unknown)}\n"
                 f"Valid codes: {', '.join(ALL_LOCALES)}")
    chosen = []
    for code in ALL_LOCALES:
        flag = section.get(code, "N").strip().upper()
        if flag not in ("Y", "N"):
            sys.exit(f"[languages] {code} = {flag!r} - must be Y or N.")
        if flag == "Y":
            chosen.append(code)
    return chosen


def text_fields(template):
    """Top-level default-language fields that hold visitor-facing text."""
    fields = {}
    for key, value in template.items():
        if not isinstance(value, str) or not value.strip() or key in NON_TEXT_FIELDS:
            continue
        if key in POLICY_TEXT_FIELDS and not TRANSLATE_POLICY_TEXT:
            continue
        fields[key] = value
    return fields


def build_locale(code, fields):
    """Return ({field: text}, [fields with no translation available])."""
    if code in ENGLISH_LOCALES:
        return dict(fields), []
    path = os.path.join(TRANSLATIONS_DIR, f"{code}.json")
    if not os.path.isfile(path):
        sys.exit(f"Missing translation file: {path}")
    with open(path, encoding="utf-8") as f:
        table = json.load(f)
    out, missing = {}, []
    for key, source in fields.items():
        if source in table:
            out[key] = table[source]
        else:
            missing.append(key)
    return out, missing


def main():
    if not CLOUD:
        sys.exit(f"[mist] cloud is required in {INI_NAME} (e.g. gc3, eu, api.mist.com).")
    api_base = cloud_to_host(CLOUD) + "/api/v1"
    locales = selected_locales()
    if not locales:
        sys.exit("No languages set to Y in [languages] - nothing to do.")

    print(f"Fetching WLAN {WLAN_ID} from {api_base} ...")
    wlan, template = fetch_template(api_base)
    print(f"  SSID: {wlan.get('ssid')}   portal auth: {wlan.get('portal', {}).get('auth')}")

    fields = text_fields(template)
    print(f"  {len(fields)} default-language text fields to translate"
          f"{'' if TRANSLATE_POLICY_TEXT else ' (policy text excluded)'}")

    new_template = json.loads(json.dumps(template))
    written = {}
    untranslated = {}
    for code in locales:
        generated, missing = build_locale(code, fields)
        existing = new_template.get(code) if isinstance(new_template.get(code), dict) else {}
        if OVERWRITE_EXISTING:
            merged = {**existing, **generated}
        else:
            merged = {**generated, **existing}
        new_template[code] = merged
        written[code] = {k: merged[k] for k in generated}
        if missing:
            untranslated[code] = missing
        print(f"  {code:8} {len(generated):3} fields" + (f", {len(missing)} untranslated" if missing else ""))

    if untranslated:
        sample = next(iter(untranslated.values()))
        print("\nSome default fields have no bundled translation (their English text "
              "differs from the stock wording), so they were left to fall back to the default:")
        for key in sample:
            print(f"    {key}: {fields[key]!r}")

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"portal_template_{WLAN_ID}_{stamp}.json")
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=1, ensure_ascii=False)
    print(f"\nBacked up current template to {backup_path}")

    if DRY_RUN:
        preview = os.path.join(BACKUP_DIR, f"portal_template_{WLAN_ID}_{stamp}_PREVIEW.json")
        with open(preview, "w", encoding="utf-8") as f:
            json.dump(new_template, f, indent=1, ensure_ascii=False)
        print(f"dry_run = Y - nothing written to Mist. Proposed template saved to {preview}")
        return

    print(f"Writing {len(locales)} language(s) to Mist ...")
    http("PUT", f"{api_base}/orgs/{ORG_ID}/wlans/{WLAN_ID}/portal_template",
         json={"portal_template": new_template})

    # A 200 doesn't prove the change stuck - re-read and compare.
    _, after = fetch_template(api_base)
    mismatches = [(code, key) for code, values in written.items()
                  for key, value in values.items()
                  if not isinstance(after.get(code), dict) or after[code].get(key) != value]
    if mismatches:
        print(f"\nWARNING: {len(mismatches)} value(s) did not persist, e.g. {mismatches[:5]}")
        print(f"The original template is in {backup_path}")
        sys.exit(1)
    print(f"Verified: all {sum(len(v) for v in written.values())} translated values persisted "
          f"across {len(written)} language(s).")


if __name__ == "__main__":
    main()
