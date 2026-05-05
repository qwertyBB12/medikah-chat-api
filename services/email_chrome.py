"""
Shared email chrome helper — mirrors live medikah.health homepage design.

Tokens, gradients, type stack, and button/eyebrow patterns are extracted
directly from the frontend's components/landing/Hero.tsx, Nav.tsx,
LandingFooter.tsx, StaggeredGrid.tsx (NOT from governance spec text —
homepage is the source of truth).

Python mirror of medikah-chat-frontend/lib/emailChrome.ts. Both files MUST
move in lockstep — divergence is a bug.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Literal

# ---------------------------------------------------------------------------
# Locked design tokens — mirror lib/emailChrome.ts:tokens
# ---------------------------------------------------------------------------
TOKENS: dict = {
    "colors": {
        # Navy / warm-gray scale
        "navyDeep": "#0D1520",
        "instBlue": "#1B2A41",
        "navyMid": "#243856",
        "navyLight": "#5A7AAA",

        # Teal scale (graduated, not flat)
        "teal700": "#1A5A68",
        "teal600": "#236B7A",
        "teal500": "#2C7A8C",
        "teal400": "#4A9AAC",
        "teal300": "#7BBFCC",
        "teal200": "#B5DDE6",

        # Linen
        "linen": "#F0EAE0",
        "linenWarm": "#E8E0D5",
        "linenLight": "#F5F1EA",
        "linenWhite": "#FAF8F4",

        # Cream (text on dark)
        "white": "#FFFFFF",
        "cream300": "#F5F0EA",
        "cream400": "#EBE4DC",
        "cream500": "#A8B4C0",

        # Light-surface text
        "deepCharcoal": "#1C1C1E",
        "bodySlate": "#4A5568",
        "textMuted": "#718096",
        "archivalGrey": "#8A8D91",

        # Hairlines / overlays
        "borderLine": "#D1D5DB",
        "hairlineDark": "rgba(27,42,65,0.06)",
        "hairlineLight": "rgba(255,255,255,0.06)",
        "overlayWhite30": "rgba(255,255,255,0.30)",
        "overlayWhite50": "rgba(255,255,255,0.50)",
        "overlayWhite60": "rgba(255,255,255,0.60)",
        "tealOverlay8": "rgba(44,122,140,0.08)",
        "tealOverlay15": "rgba(44,122,140,0.15)",

        # Semantic
        "success": "#2D7D5F",
        "warning": "#B8860B",
        "error": "#B83D3D",

        # Compat aliases (old keys → new keys)
        "clinicalTeal": "#2C7A8C",
        "creamOnDark": "#F5F0EA",
    },
    "fonts": {
        "body": "'Mulish', -apple-system, 'Segoe UI', Arial, sans-serif",
        "heading": "'Oswald', 'Arial Narrow', Arial, sans-serif",
        # Compat aliases (old keys point to body)
        "ui": "'Mulish', -apple-system, 'Segoe UI', Arial, sans-serif",
        "accent": "'Mulish', -apple-system, 'Segoe UI', Arial, sans-serif",
        "display": "'Oswald', 'Arial Narrow', Arial, sans-serif",
    },
    "radii": {
        "sm": "8px",
        "md": "16px",
        "lg": "24px",
        "xl": "32px",
    },
    "gradients": {
        "navy": "linear-gradient(180deg,#1B2A41 0%,#0D1520 100%)",
        "linenWarm": "linear-gradient(135deg,#F5F1EA 0%,#E8E0D5 100%)",
        "tealSoft": "linear-gradient(135deg,#B5DDE6 0%,#E8E0D5 100%)",
    },
    # Page background — parchment. Mirrors the homepage's StaggeredGrid CARD
    # surface (#FAF8F4 linen-white), where users actually read text. Cool-cream
    # for clinical readability while staying on-brand.
    "pageBg": "#FAF8F4",  # linen-white — parchment body
}


# ---------------------------------------------------------------------------
# asset_url
# ---------------------------------------------------------------------------
def asset_url(relative_path: str) -> str:
    base = os.environ.get("BASE_URL", "https://medikah.health")
    clean_base = base.rstrip("/")
    clean_path = relative_path if relative_path.startswith("/") else f"/{relative_path}"
    return f"{clean_base}{clean_path}"


# ---------------------------------------------------------------------------
# email_head — Mulish + Oswald only (DM Sans/DM Serif do not appear on homepage)
# ---------------------------------------------------------------------------
def email_head() -> str:
    teal = TOKENS["colors"]["teal500"]
    return (
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        '<meta name="x-apple-disable-message-reformatting">\n'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">\n'
        '<meta name="color-scheme" content="light">\n'
        '<meta name="supported-color-schemes" content="light">\n'
        '<title>Medikah</title>\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link href="https://fonts.googleapis.com/css2?family=Mulish:wght@400;500;600;700;800;900&family=Oswald:wght@300;400;500;600;700&display=swap" rel="stylesheet">\n'
        '<style>\n'
        '  body { margin:0; padding:0; -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }\n'
        '  table { border-collapse:collapse !important; }\n'
        '  img { border:0; outline:none; text-decoration:none; -ms-interpolation-mode:bicubic; display:block; }\n'
        f'  a {{ color:{teal}; text-decoration:none; }}\n'
        '  @media only screen and (max-width:600px) {\n'
        '    .email-container { width:100% !important; }\n'
        '    .email-pad { padding:32px 24px !important; }\n'
        '    .email-h1 { font-size:32px !important; line-height:1.05 !important; }\n'
        '  }\n'
        '</style>\n'
        '</head>'
    )


# ---------------------------------------------------------------------------
# email_curve_divider — port of components/landing/CurveDivider.tsx.
# 40px-tall wave between sections. Inline SVG; Outlook desktop strips SVG and
# falls back to the flat container bg (clean ~40px transition band).
# ---------------------------------------------------------------------------
def email_curve_divider(from_color: str, bg_color: str, flip: bool = False) -> str:
    if flip:
        d = "M0,0 C480,40 960,40 1440,0 L1440,40 L0,40 Z"
        container_bg = from_color
        path_fill = bg_color
    else:
        d = "M0,40 C480,0 960,0 1440,40 L1440,0 L0,0 Z"
        container_bg = bg_color
        path_fill = from_color
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{container_bg};">\n'
        f'  <tr>\n'
        f'    <td height="40" style="height:40px;line-height:0;font-size:0;mso-line-height-rule:exactly;padding:0;">\n'
        f'      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1440 40" preserveAspectRatio="none" width="100%" height="40" style="display:block;width:100%;height:40px;">\n'
        f'        <path d="{d}" fill="{path_fill}"/>\n'
        f'      </svg>\n'
        f'    </td>\n'
        f'  </tr>\n'
        f'</table>'
    )


# ---------------------------------------------------------------------------
# email_section_label — homepage section-header pattern (StaggeredGrid).
# 48px teal-500 hairline + gap + eyebrow text, side-by-side. Use at the TOP
# of every editorial section.
# ---------------------------------------------------------------------------
def email_section_label(text: str, variant: str = "light", margin_bottom: str = "20px") -> str:
    color = TOKENS["colors"]["teal400"] if variant == "dark" else TOKENS["colors"]["teal500"]
    body = TOKENS["fonts"]["body"]
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 {margin_bottom} 0;">\n'
        f'  <tr>\n'
        f'    <td height="1" width="48" style="height:1px;width:48px;background-color:{color};font-size:0;line-height:1px;">&nbsp;</td>\n'
        f'    <td width="16" style="width:16px;font-size:0;line-height:0;">&nbsp;</td>\n'
        f'    <td style="font-family:{body};font-size:11px;font-weight:500;text-transform:uppercase;letter-spacing:0.25em;color:{color};line-height:1.2;">{text}</td>\n'
        f'  </tr>\n'
        f'</table>'
    )


# ---------------------------------------------------------------------------
# email_eyebrow — homepage signature label pattern
# ---------------------------------------------------------------------------
def email_eyebrow(text: str, variant: str = "light", margin_bottom: str = "16px") -> str:
    color = TOKENS["colors"]["teal400"] if variant == "dark" else TOKENS["colors"]["teal500"]
    body = TOKENS["fonts"]["body"]
    return (
        f'<p style="font-family:{body};font-size:11px;font-weight:500;'
        f'text-transform:uppercase;letter-spacing:0.25em;color:{color};'
        f'margin:0 0 {margin_bottom} 0;line-height:1.2;">{text}</p>'
    )


# ---------------------------------------------------------------------------
# email_heading — Oswald uppercase display headline
# ---------------------------------------------------------------------------
def email_heading(text: str, variant: str = "light", size: int = 38, level: int = 1) -> str:
    color = TOKENS["colors"]["white"] if variant == "dark" else TOKENS["colors"]["deepCharcoal"]
    heading = TOKENS["fonts"]["heading"]
    return (
        f'<h{level} class="email-h1" style="font-family:{heading};font-weight:500;'
        f'text-transform:uppercase;letter-spacing:-0.02em;line-height:0.95;'
        f'color:{color};font-size:{size}px;margin:0;">{text}</h{level}>'
    )


# ---------------------------------------------------------------------------
# email_button — homepage CTA pattern
# ---------------------------------------------------------------------------
def email_button(label: str, href: str, variant: str = "primary") -> str:
    C = TOKENS["colors"]
    R = TOKENS["radii"]
    body = TOKENS["fonts"]["body"]
    if variant == "primary":
        bg = C["teal500"]
        color = C["white"]
        border = f'2px solid {C["teal500"]}'
    elif variant == "secondary-dark":
        bg = "transparent"
        color = C["white"]
        border = f'2px solid {C["overlayWhite30"]}'
    else:  # secondary-light
        bg = "transparent"
        color = C["instBlue"]
        border = f'2px solid {C["borderLine"]}'
    bg_attr = bg if bg != "transparent" else "transparent"
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="display:inline-block;">\n'
        f'  <tr>\n'
        f'    <td style="border-radius:{R["lg"]};background-color:{bg_attr};">\n'
        f'      <a href="{href}" style="display:inline-block;font-family:{body};font-size:13px;'
        f'font-weight:500;text-transform:uppercase;letter-spacing:0.04em;color:{color};'
        f'text-decoration:none;padding:14px 36px;border:{border};border-radius:{R["lg"]};">{label} &rarr;</a>\n'
        f'    </td>\n'
        f'  </tr>\n'
        f'</table>'
    )


# ---------------------------------------------------------------------------
# email_header — Nav-style masthead w/ navy gradient + logo + lowercase wordmark
# ---------------------------------------------------------------------------
Variant = Literal["navy", "linen"]
Locale = Literal["en", "es"]
Wordmark = Literal["medikah", "practikah"]


def email_header(
    variant: Variant,
    locale: Locale,
    wordmark: Wordmark = "medikah",
    eyebrow: str | None = None,
) -> str:
    C = TOKENS["colors"]
    body = TOKENS["fonts"]["body"]
    grad = TOKENS["gradients"]["navy"]
    wm_text = "pr&aacute;ctikah" if wordmark == "practikah" else "medikah"

    page_bg = TOKENS["pageBg"]
    if variant == "navy":
        logo = asset_url("/logo.png")
        eyebrow_html = (
            f'<tr><td align="center" style="padding-bottom:14px;">{email_eyebrow(eyebrow, "dark", "0")}</td></tr>'
            if eyebrow else ""
        )
        # Navy gradient masthead + wave divider down into the sand bg.
        curve_down = email_curve_divider(C["navyDeep"], page_bg)
        return (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">\n'
            f'  <tr>\n'
            f'    <td style="padding:0;background-color:{C["instBlue"]};background-image:{grad};">\n'
            f'      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">\n'
            f'        <tr>\n'
            f'          <td align="center" style="padding:36px 32px 40px 32px;">\n'
            f'            <table role="presentation" cellpadding="0" cellspacing="0" border="0">\n'
            f'              {eyebrow_html}\n'
            f'              <tr><td align="center">\n'
            f'                <table role="presentation" cellpadding="0" cellspacing="0" border="0">\n'
            f'                  <tr>\n'
            f'                    <td style="vertical-align:middle;padding-right:10px;line-height:0;">\n'
            f'                      <img src="{logo}" alt="" width="24" height="24" style="display:block;width:24px;height:24px;border:0;opacity:0.7;">\n'
            f'                    </td>\n'
            f'                    <td style="vertical-align:middle;">\n'
            f'                      <span style="font-family:{body};font-weight:400;font-size:22px;letter-spacing:0.04em;color:{C["white"]};text-transform:lowercase;">{wm_text}</span>\n'
            f'                    </td>\n'
            f'                  </tr>\n'
            f'                </table>\n'
            f'              </td></tr>\n'
            f'            </table>\n'
            f'          </td>\n'
            f'        </tr>\n'
            f'      </table>\n'
            f'    </td>\n'
            f'  </tr>\n'
            f'  <tr><td style="padding:0;">{curve_down}</td></tr>\n'
            f'  <!-- locale stamp: {locale} -->\n'
            f'</table>'
        )

    # Linen variant — Nav-style masthead on linen bg (mirrors homepage Nav
    # scrolled state: navy logo + lowercase Mulish wordmark in navy).
    logo_dark = asset_url("/logo-BLU.png")
    eyebrow_html_light = (
        f'<tr><td align="center" style="padding-bottom:14px;">{email_eyebrow(eyebrow, "light", "0")}</td></tr>'
        if eyebrow else ""
    )
    curve_down_light = email_curve_divider(C["linen"], page_bg)
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">\n'
        f'  <tr>\n'
        f'    <td style="padding:0;background-color:{C["linen"]};">\n'
        f'      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">\n'
        f'        <tr>\n'
        f'          <td align="center" style="padding:36px 32px 40px 32px;">\n'
        f'            <table role="presentation" cellpadding="0" cellspacing="0" border="0">\n'
        f'              {eyebrow_html_light}\n'
        f'              <tr><td align="center">\n'
        f'                <table role="presentation" cellpadding="0" cellspacing="0" border="0">\n'
        f'                  <tr>\n'
        f'                    <td style="vertical-align:middle;padding-right:10px;line-height:0;">\n'
        f'                      <img src="{logo_dark}" alt="" width="24" height="24" style="display:block;width:24px;height:24px;border:0;opacity:0.85;">\n'
        f'                    </td>\n'
        f'                    <td style="vertical-align:middle;">\n'
        f'                      <span style="font-family:{body};font-weight:400;font-size:22px;letter-spacing:0.04em;color:{C["instBlue"]};text-transform:lowercase;">{wm_text}</span>\n'
        f'                    </td>\n'
        f'                  </tr>\n'
        f'                </table>\n'
        f'              </td></tr>\n'
        f'            </table>\n'
        f'          </td>\n'
        f'        </tr>\n'
        f'      </table>\n'
        f'    </td>\n'
        f'  </tr>\n'
        f'  <tr><td style="padding:0;">{curve_down_light}</td></tr>\n'
        f'  <!-- locale stamp: {locale} -->\n'
        f'</table>'
    )


# ---------------------------------------------------------------------------
# email_footer — homepage Footer pattern: navy gradient + rounded-top
# ---------------------------------------------------------------------------
def email_footer(locale: Locale) -> str:
    C = TOKENS["colors"]
    body = TOKENS["fonts"]["body"]

    if locale == "es":
        tagline = "Cuidado humano sin distancia"
        privacy_label = "Privacidad"
        terms_label = "T&eacute;rminos"
        contact_label = "Contacto"
    else:
        tagline = "Human care without distance"
        privacy_label = "Privacy"
        terms_label = "Terms"
        contact_label = "Contact"
    copyright_text = f"&copy; {_dt.datetime.now().year} Medikah Corporation"

    # Letter-style signature on linen — no navy slab. Centered stack.
    page_bg = TOKENS["pageBg"]
    curve_up = email_curve_divider(page_bg, C["linen"], flip=True)
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">\n'
        f'  <tr><td style="padding:0;">{curve_up}</td></tr>\n'
        f'  <tr>\n'
        f'    <td align="center" style="padding:32px 32px 36px 32px;background-color:{C["linen"]};">\n'
        f'      <table role="presentation" cellpadding="0" cellspacing="0" border="0">\n'
        f'        <tr>\n'
        f'          <td align="center" style="font-family:{body};">\n'
        f'            <p style="margin:0;font-size:18px;font-weight:400;color:{C["instBlue"]};text-transform:lowercase;letter-spacing:0.04em;line-height:1;">medikah</p>\n'
        f'            <p style="margin:8px 0 0 0;font-family:{body};font-size:11px;font-weight:500;letter-spacing:0.04em;color:{C["teal500"]};">{tagline}</p>\n'
        f'          </td>\n'
        f'        </tr>\n'
        f'        <tr>\n'
        f'          <td align="center" style="padding-top:20px;">\n'
        f'            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 auto;">\n'
        f'              <tr><td height="1" width="48" style="height:1px;width:48px;background-color:{C["hairlineDark"]};font-size:0;line-height:1px;">&nbsp;</td></tr>\n'
        f'            </table>\n'
        f'          </td>\n'
        f'        </tr>\n'
        f'        <tr>\n'
        f'          <td align="center" style="padding-top:18px;font-family:{body};">\n'
        f'            <a href="https://medikah.health/privacy" style="font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:0.12em;color:{C["archivalGrey"]};text-decoration:none;">{privacy_label}</a>\n'
        f'            <span style="font-size:10px;color:{C["archivalGrey"]};margin:0 10px;">&middot;</span>\n'
        f'            <a href="https://medikah.health/terms" style="font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:0.12em;color:{C["archivalGrey"]};text-decoration:none;">{terms_label}</a>\n'
        f'            <span style="font-size:10px;color:{C["archivalGrey"]};margin:0 10px;">&middot;</span>\n'
        f'            <a href="mailto:hello@medikah.health" style="font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:0.12em;color:{C["archivalGrey"]};text-decoration:none;">{contact_label}</a>\n'
        f'          </td>\n'
        f'        </tr>\n'
        f'        <tr>\n'
        f'          <td align="center" style="padding-top:12px;">\n'
        f'            <p style="margin:0;font-family:{body};font-size:10px;font-weight:400;letter-spacing:0.04em;color:{C["archivalGrey"]};">{copyright_text}</p>\n'
        f'          </td>\n'
        f'        </tr>\n'
        f'      </table>\n'
        f'    </td>\n'
        f'  </tr>\n'
        f'</table>'
    )


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------
def email_shell_open(
    variant: Variant,
    locale: Locale,
    wordmark: Wordmark = "medikah",
    page_bg: str | None = None,
    eyebrow: str | None = None,
) -> str:
    bg = page_bg or TOKENS["pageBg"]
    body = TOKENS["fonts"]["body"]
    slate = TOKENS["colors"]["bodySlate"]
    return (
        '<!DOCTYPE html>\n'
        f'<html lang="{locale}">\n'
        f'{email_head()}\n'
        f'<body style="margin:0;padding:0;background-color:{bg};font-family:{body};color:{slate};">\n'
        f'{email_header(variant, locale, wordmark, eyebrow)}'
    )


def email_shell_close(locale: Locale) -> str:
    return f'{email_footer(locale)}\n</body>\n</html>'
