"""Product branding: name, logo, credits and shared styling."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

APP_NAME = "ResearchMate Literature Review"
APP_TAGLINE = "Evidence-linked literature reviews from your own PDFs"
OWNER = "ResearchMate Academy"
OWNER_PERSON = "Dr Abdollah Baghaei"
DEVELOPER = "Tech Innovation Experts"
DEVELOPER_URL = "https://www.tinx.co.nz"
DEVELOPER_DOMAIN = "www.tinx.co.nz"

ASSETS = Path(__file__).resolve().parents[2] / "assets"
LOGO = ASSETS / "tinx_logo.png"
ICON = ASSETS / "tinx_icon.png"

CREDIT_TEXT = f"Designed and powered by {DEVELOPER} · {DEVELOPER_DOMAIN} · Owner: {OWNER}: {OWNER_PERSON}"

_CSS = """
<style>
  .rm-footer {
    margin-top: 3rem; padding-top: 1rem; border-top: 1px solid rgba(49, 51, 63, 0.15);
    font-size: 0.82rem; color: rgba(49, 51, 63, 0.7); text-align: center; line-height: 1.6;
  }
  .rm-footer a { color: inherit; text-decoration: underline; }
  .rm-sidebar-credit { font-size: 0.75rem; color: rgba(49, 51, 63, 0.7); line-height: 1.5; }
  .rm-sidebar-credit a { color: inherit; }
  .rm-hero h1 { margin-bottom: 0.2rem; }
  .rm-hero p { font-size: 1.05rem; color: rgba(49, 51, 63, 0.8); margin-top: 0; }
</style>
"""


def page_icon():
    """Favicon: the logo's icon mark, or a fallback emoji if the asset is missing."""
    if ICON.exists():
        from PIL import Image

        return Image.open(ICON)
    return "📚"


def apply_branding() -> None:
    """Sidebar logo (links to the developer's site) and shared CSS. Call once per script run."""
    if LOGO.exists():
        st.logo(str(LOGO), size="large", link=DEVELOPER_URL, icon_image=str(ICON) if ICON.exists() else None)
    st.markdown(_CSS, unsafe_allow_html=True)


def sidebar_credit() -> None:
    st.sidebar.markdown(
        f"<div class='rm-sidebar-credit'>Designed and powered by "
        f"<a href='{DEVELOPER_URL}' target='_blank'>{DEVELOPER}</a> · "
        f"<a href='{DEVELOPER_URL}' target='_blank'>{DEVELOPER_DOMAIN}</a><br>"
        f"Owner: {OWNER}: {OWNER_PERSON}</div>",
        unsafe_allow_html=True,
    )


def footer() -> None:
    st.markdown(
        f"<div class='rm-footer'><strong>{APP_NAME}</strong><br>"
        f"Designed and powered by <a href='{DEVELOPER_URL}' target='_blank'>{DEVELOPER}</a> · "
        f"<a href='{DEVELOPER_URL}' target='_blank'>{DEVELOPER_DOMAIN}</a><br>"
        f"Owner: {OWNER}: {OWNER_PERSON}</div>",
        unsafe_allow_html=True,
    )


def hero(title: str, subtitle: str) -> None:
    st.markdown(f"<div class='rm-hero'><h1>{title}</h1><p>{subtitle}</p></div>", unsafe_allow_html=True)
