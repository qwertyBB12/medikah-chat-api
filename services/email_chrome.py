"""
Shared email chrome helper — locked governance tokens for transactional email.

Python mirror of medikah-chat-frontend/lib/emailChrome.ts. Same tokens, same
HTML output. All inline styles (email clients strip <style> blocks and class
attributes). Logo URLs are absolute https because the backend has no Next.js
asset pipeline — patients open these emails outside Next.js.

Source of truth for tokens: CLAUDE.md "Approved Design Overrides" +
medikah-chat-frontend/tailwind.config.js. If TOKENS drifts from the frontend
helper, that's a bug — both must update in lockstep.
"""

from __future__ import annotations

import os
from typing import Literal

# ---------------------------------------------------------------------------
# Locked design tokens (mirrors lib/emailChrome.ts:tokens)
# ---------------------------------------------------------------------------
TOKENS: dict = {
    "colors": {
        "instBlue": "#1B2A41",       # Institutional Navy — primary brand
        "clinicalTeal": "#2C7A8C",   # Accent / link / secondary CTA
        "linen": "#F0EAE0",          # Warm body bg
        "linenWhite": "#FAF8F4",     # Default page bg
        "deepCharcoal": "#1C1C1E",   # Headlines on light
        "bodySlate": "#4A5568",      # Body text
        "borderLine": "#D1D5DB",     # Hairlines
        "white": "#FFFFFF",
        "creamOnDark": "#F5F0EA",    # Text on navy
        "success": "#2D7D5F",
        "warning": "#B8860B",
        "error": "#B83D3D",
    },
    "fonts": {
        "display": "'Oswald', 'Arial Narrow', Arial, sans-serif",
        "ui": "'DM Sans', -apple-system, 'Segoe UI', Arial, sans-serif",
        "accent": "'DM Serif Display', Georgia, 'Times New Roman', serif",
        "body": "'Mulish', -apple-system, 'Segoe UI', Arial, sans-serif",
    },
    "radii": {
        "sm": "8px",
        "md": "16px",
        "lg": "24px",
        "xl": "32px",
    },
    "pageBg": "#FAF8F4",
}


# ---------------------------------------------------------------------------
# Asset URL — always absolute https for email clients
# ---------------------------------------------------------------------------
def asset_url(relative_path: str) -> str:
    """Resolve a relative asset path to an absolute https URL.

    Backend has no Next.js public-asset pipeline, so we resolve via BASE_URL
    env var with a hardcoded medikah.health fallback. Trailing slashes on
    the base and missing leading slashes on the path are normalized.
    """
    base = os.environ.get("BASE_URL", "https://medikah.health")
    clean_base = base.rstrip("/")
    clean_path = relative_path if relative_path.startswith("/") else f"/{relative_path}"
    return f"{clean_base}{clean_path}"


# ---------------------------------------------------------------------------
# email_head — returns a <head> block with Google Fonts + viewport + reset
# ---------------------------------------------------------------------------
def email_head() -> str:
    teal = TOKENS["colors"]["clinicalTeal"]
    return (
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        '<meta name="x-apple-disable-message-reformatting">\n'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">\n'
        '<title>Medikah</title>\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=DM+Serif+Display&family=Mulish:wght@300;400;600;700;800;900&family=Oswald:wght@300;400;500;600;700&display=swap" rel="stylesheet">\n'
        '<style>\n'
        '  body { margin:0; padding:0; -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }\n'
        '  table { border-collapse:collapse !important; }\n'
        '  img { border:0; outline:none; text-decoration:none; -ms-interpolation-mode:bicubic; display:block; }\n'
        f'  a {{ color:{teal}; text-decoration:none; }}\n'
        '  @media only screen and (max-width:600px) {\n'
        '    .email-container { width:100% !important; }\n'
        '    .email-pad { padding:24px !important; }\n'
        '  }\n'
        '</style>\n'
        '</head>'
    )


# ---------------------------------------------------------------------------
# email_header — masthead band with logo + wordmark
# ---------------------------------------------------------------------------
Variant = Literal["navy", "linen"]
Locale = Literal["en", "es"]
Wordmark = Literal["medikah", "practikah"]


def email_header(
    variant: Variant,
    locale: Locale,
    wordmark: Wordmark = "medikah",
) -> str:
    """Render the masthead band.

    `locale` is reserved for future bilingual subhead text — current implementation
    matches lib/emailChrome.ts:emailHeader (no locale-specific text in the masthead;
    it lives in the body).
    """
    _ = locale  # parity with TS helper signature; reserved for future use
    nav = TOKENS["colors"]["instBlue"]
    linen = TOKENS["colors"]["linen"]
    border = TOKENS["colors"]["borderLine"]
    body_font = TOKENS["fonts"]["body"]

    if variant == "navy":
        logo = asset_url("/logo.png")
        wm = asset_url("/medikah_wht.png")
        wm_alt = "Práctikah" if wordmark == "practikah" else "medikah"
        return (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{nav};">\n'
            '  <tr>\n'
            '    <td align="center" style="padding:28px 32px;">\n'
            '      <table role="presentation" cellpadding="0" cellspacing="0">\n'
            '        <tr>\n'
            '          <td style="vertical-align:middle;padding-right:14px;">\n'
            f'            <img src="{logo}" alt="Medikah" width="40" height="40" style="display:block;width:40px;height:40px;border:0;">\n'
            '          </td>\n'
            '          <td style="vertical-align:middle;">\n'
            f'            <img src="{wm}" alt="{wm_alt}" height="22" style="display:block;height:22px;border:0;">\n'
            '          </td>\n'
            '        </tr>\n'
            '      </table>\n'
            '    </td>\n'
            '  </tr>\n'
            '</table>'
        )

    # linen variant: navy logo + Mulish lowercase wordmark text in navy
    logo_dark = asset_url("/logo-BLU.png")
    wm_text = "pr&aacute;ctikah" if wordmark == "practikah" else "medikah"
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{linen};border-bottom:1px solid {border};">\n'
        '  <tr>\n'
        '    <td align="center" style="padding:28px 32px;">\n'
        '      <table role="presentation" cellpadding="0" cellspacing="0">\n'
        '        <tr>\n'
        '          <td style="vertical-align:middle;padding-right:14px;">\n'
        f'            <img src="{logo_dark}" alt="Medikah" width="40" height="40" style="display:block;width:40px;height:40px;border:0;">\n'
        '          </td>\n'
        '          <td style="vertical-align:middle;">\n'
        f'            <span style="font-family:{body_font};font-weight:800;font-size:26px;letter-spacing:-0.01em;color:{nav};">{wm_text}</span>\n'
        '          </td>\n'
        '        </tr>\n'
        '      </table>\n'
        '    </td>\n'
        '  </tr>\n'
        '</table>'
    )


# ---------------------------------------------------------------------------
# email_footer — bilingual footer band
# ---------------------------------------------------------------------------
def email_footer(locale: Locale) -> str:
    nav = TOKENS["colors"]["instBlue"]
    linen = TOKENS["colors"]["linen"]
    border = TOKENS["colors"]["borderLine"]
    slate = TOKENS["colors"]["bodySlate"]
    body_font = TOKENS["fonts"]["body"]

    if locale == "es":
        tagline = "Cuidado Sin Distancia · medikah.health"
        incorporation = "Medikah Corporation · Constituida en Delaware, EE. UU."
        privacy_label = "Aviso de Privacidad"
        terms_label = "Términos del Servicio"
    else:
        tagline = "Care Without Distance · medikah.health"
        incorporation = "Medikah Corporation · Incorporated in Delaware, USA"
        privacy_label = "Privacy Policy"
        terms_label = "Terms of Service"

    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{linen};border-top:1px solid {border};">\n'
        '  <tr>\n'
        f'    <td align="center" style="padding:24px 32px;font-family:{body_font};">\n'
        f'      <p style="margin:0 0 12px 0;font-size:14px;color:{nav};font-weight:700;letter-spacing:0.02em;">{tagline}</p>\n'
        f'      <p style="margin:0 0 12px 0;font-size:13px;line-height:1.5;color:{slate};">{incorporation}</p>\n'
        '      <p style="margin:0;font-size:13px;">\n'
        f'        <a href="https://medikah.health/privacy" style="color:{nav};text-decoration:none;font-weight:600;">{privacy_label}</a>\n'
        f'        <span style="color:{border};margin:0 8px;">|</span>\n'
        f'        <a href="https://medikah.health/terms" style="color:{nav};text-decoration:none;font-weight:600;">{terms_label}</a>\n'
        '      </p>\n'
        '    </td>\n'
        '  </tr>\n'
        '</table>'
    )


# ---------------------------------------------------------------------------
# email_shell_open / email_shell_close — convenience wrappers
# ---------------------------------------------------------------------------
def email_shell_open(
    variant: Variant,
    locale: Locale,
    wordmark: Wordmark = "medikah",
    page_bg: str | None = None,
) -> str:
    bg = page_bg or TOKENS["pageBg"]
    body_font = TOKENS["fonts"]["body"]
    slate = TOKENS["colors"]["bodySlate"]
    return (
        '<!DOCTYPE html>\n'
        f'<html lang="{locale}">\n'
        f'{email_head()}\n'
        f'<body style="margin:0;padding:0;background-color:{bg};font-family:{body_font};color:{slate};">\n'
        f'{email_header(variant, locale, wordmark)}'
    )


def email_shell_close(locale: Locale) -> str:
    return f'{email_footer(locale)}\n</body>\n</html>'
