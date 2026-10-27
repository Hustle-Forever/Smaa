"""Source guards for the "Producer" (campaigns) tab wiring.

Adding a tab to this app means editing five separate places, and only ONE of
them is protected by the type checker (`TAB_LABELS` is a `Record<Tab, string>`,
so a missing label fails `tsc`). The dangerous one is `VALID_TABS` in App.tsx: it
is a hand-maintained array with no exhaustiveness link to the `Tab` union, so a
union member missing from it compiles cleanly and then silently falls back to the
dashboard at runtime. These tests are the safety net for that gap.

Frontend source guards from Python match this suite's existing convention (see
test_poster_delete.py) — no node, no browser, just the files.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_REPO = _BACKEND.parent
TAB_ID = "campaigns"


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_tab_is_in_the_union():
    src = _read("frontend/src/App.tsx")
    union = src.split("export type Tab =", 1)[1].split(";", 1)[0]
    assert f'"{TAB_ID}"' in union, "campaigns missing from the Tab union"


def test_tab_is_in_valid_tabs():
    """The silent-failure spot: absent here, #campaigns resolves to #dashboard."""
    src = _read("frontend/src/App.tsx")
    arr = src.split("const VALID_TABS: Tab[] = [", 1)[1].split("]", 1)[0]
    assert f'"{TAB_ID}"' in arr, (
        "campaigns missing from VALID_TABS — the tab would silently fall back "
        "to the dashboard, with no type error to warn you."
    )


def test_union_and_valid_tabs_agree():
    """Guards the whole app, not just this feature: the two lists must match."""
    src = _read("frontend/src/App.tsx")
    union = set(re.findall(r'"([a-z-]+)"', src.split("export type Tab =", 1)[1].split(";", 1)[0]))
    arr = set(re.findall(r'"([a-z-]+)"', src.split("const VALID_TABS: Tab[] = [", 1)[1].split("]", 1)[0]))
    assert union == arr, (
        f"Tab union and VALID_TABS disagree — only in union: {union - arr}; "
        f"only in VALID_TABS: {arr - union}"
    )


def test_tab_has_a_sidebar_label():
    src = _read("frontend/src/components/Sidebar.tsx")
    assert f'id: "{TAB_ID}"' in src, "campaigns has no sidebar nav entry"
    assert 'label: "Producer"' in src
    assert 'badge: "beta"' in src, "the beta chip is part of the ask"


def test_sidebar_renders_the_badge():
    """A `badge` field nobody renders would be a silent no-op."""
    src = _read("frontend/src/components/Sidebar.tsx")
    assert "badge &&" in src or "badge ?" in src, "NavItem.badge is never rendered"


def test_ideation_label_is_untouched():
    """`producer` (the hands-on studio) keeps its own name — two distinct tabs."""
    assert 'label: "Ideation"' in _read("frontend/src/components/Sidebar.tsx")
    assert 'producer: "Ideation"' in _read("frontend/src/components/Layout.tsx")


def test_tab_has_a_layout_label():
    assert f'{TAB_ID}: "Producer"' in _read("frontend/src/components/Layout.tsx")


def test_page_renders_in_children_not_the_persistent_slot():
    """The persistent slot exists to keep in-flight CLIENT state alive across
    navigation. Campaign state is entirely server-side, so this page must remount
    normally — putting it in the slot would keep its poll timer running forever
    on every other tab."""
    src = _read("frontend/src/App.tsx")
    assert f'tab === "{TAB_ID}" && (' in src, "no render branch for campaigns"
    persistent = src.split("persistent={", 1)[1].split("      >", 1)[0]
    assert TAB_ID not in persistent, (
        "campaigns must not render in the persistent slot — its poll loop would "
        "keep running on every other tab."
    )


def test_deep_link_passes_the_route_param():
    """#campaigns/<id> opens one campaign."""
    src = _read("frontend/src/App.tsx")
    branch = src.split(f'tab === "{TAB_ID}" && (', 1)[1].split(")}", 1)[0]
    assert "campaignId={routeParam}" in branch


# --- the API namespace + types are hand-synced to the backend ---------------

def _strip_comments(ts: str) -> str:
    """Drop // and /* */ comments so a guard can't trip over its own prose."""
    ts = re.sub(r"/\*.*?\*/", "", ts, flags=re.S)
    return re.sub(r"//[^\n]*", "", ts)


def test_api_namespace_exists_and_cannot_publish():
    """Gate 2 must stay on the single roadmap publish path. If this namespace
    grew a publish call there would be two publishers and a double post becomes
    possible."""
    src = _read("frontend/src/lib/api.ts")
    assert "campaigns: {" in src, "no campaigns namespace in api.ts"
    ns = _strip_comments(src.split("campaigns: {", 1)[1].split("\n};", 1)[0]).lower()
    for verb in ("publish", "/post\"", "instagram"):
        assert verb not in ns, (
            f"the campaigns API namespace must not contain {verb!r} — publishing "
            f"belongs to the one pre-existing roadmap endpoint."
        )


def test_api_has_both_gate1_verbs():
    src = _read("frontend/src/lib/api.ts")
    ns = src.split("campaigns: {", 1)[1].split("\n};", 1)[0]
    for fn in ("approve:", "decline:", "runCycle:", "reviewQueue:", "summary:"):
        assert fn in ns, f"api.campaigns.{fn} missing"


def test_item_status_union_matches_the_backend():
    """types.ts is hand-written with no codegen, so a status the backend can emit
    but the union lacks would only fail at runtime."""
    from app.services import campaign_store as cs

    ts = _strip_comments(_read("frontend/src/lib/types.ts"))
    union = ts.split("export type CampaignItemStatus =", 1)[1].split(";", 1)[0]
    backend = {
        cs.ITEM_PLANNED, cs.ITEM_GENERATING, cs.ITEM_READY, cs.ITEM_APPROVED,
        cs.ITEM_SCHEDULED, cs.ITEM_PUBLISHED, cs.ITEM_REJECTED, cs.ITEM_FAILED,
        cs.ITEM_PUBLISH_FAILED, cs.ITEM_SKIPPED, cs.ITEM_EXPIRED, cs.ITEM_CANCELLED,
    }
    for status in backend:
        assert f'"{status}"' in union, f"CampaignItemStatus is missing {status!r}"


def test_every_status_has_ui_copy():
    """A status with no entry in STATUS_META would render an empty pill."""
    from app.services import campaign_store as cs

    page = _read("frontend/src/pages/Campaigns.tsx")
    meta = page.split("STATUS_META: Record<CampaignItemStatus", 1)[1].split("};", 1)[0]
    for status in (cs.ITEM_PLANNED, cs.ITEM_GENERATING, cs.ITEM_READY, cs.ITEM_APPROVED,
                   cs.ITEM_SCHEDULED, cs.ITEM_PUBLISHED, cs.ITEM_REJECTED, cs.ITEM_FAILED,
                   cs.ITEM_PUBLISH_FAILED, cs.ITEM_SKIPPED, cs.ITEM_EXPIRED,
                   cs.ITEM_CANCELLED):
        assert f"{status}:" in meta, f"no label/colour for {status!r}"


# --- UI conventions --------------------------------------------------------

def test_wizard_portals_with_esc_and_scroll_lock():
    """The page-transition transform traps position:fixed, so a modal MUST
    portal to document.body or it renders inside the content column.

    The Escape key and the scroll lock used to be written out inline here, and
    this test read them off the wizard. They now live in `useModalChrome`, which
    every dialog shares, so the contract is checked where it is implemented and
    the wizard is only checked for still using it. Pinning the old inline
    strings made this test fail the moment the duplication was removed, which is
    backwards: the refactor was the improvement.
    """
    src = _read("frontend/src/components/campaigns/CampaignWizard.tsx")
    assert "createPortal(" in src and "document.body" in src
    assert "zIndex: 110" in src, "shared modal layer"
    assert "useModalChrome" in src, "the dialog keyboard contract comes from the hook"

    chrome = _read("frontend/src/lib/useModalChrome.ts")
    assert 'e.key === "Escape"' in chrome
    assert 'document.body.style.overflow = "hidden"' in chrome
    assert "const prevOverflow = document.body.style.overflow" in chrome, (
        "restore the PREVIOUS overflow, don't clear it"
    )
    assert "document.body.style.overflow = prevOverflow" in chrome, (
        "and put it back on the way out"
    )


def test_review_card_uses_the_shared_thumb():
    """The two big grids already fork Thumb; new code should not add a third
    hand-rolled image tile."""
    src = _read("frontend/src/components/campaigns/ReviewCard.tsx")
    assert "from \"../Thumb\"" in src and "<Thumb" in src


def test_polling_is_gated_on_work_in_flight():
    """No timer at all when nothing is generating — the idiom the creators use."""
    src = _read("frontend/src/pages/Campaigns.tsx")
    assert "if (!busyKey) return;" in src, "poll must not run when idle"
    assert "cancelled = true" in src, "post-await guard on cleanup"
    assert "window.clearInterval" in src


def test_page_tells_the_user_a_human_still_posts():
    """The two-gate promise has to be visible in the product, not just the code."""
    page = _read("frontend/src/pages/Campaigns.tsx")
    assert "Nothing is posted without you." in page
    card = _read("frontend/src/components/campaigns/ReviewCard.tsx")
    assert "You still press Post" in card
