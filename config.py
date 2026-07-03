"""
config.py — Central configuration for the Abbey receiving-desk agent.

Everything tunable lives here so a non-developer can adjust behaviour without
touching the logic. Values can be overridden by a local `abbey_config.json`
file (created automatically the first time you save settings from the app) and
by environment variables for secrets.

NOTHING secret should be hard-coded here. The Anthropic API key is read from the
environment variable ANTHROPIC_API_KEY (see SETUP_GUIDE.md).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PHOTO_DIR = DATA_DIR / "photos"
EXPORT_DIR = DATA_DIR / "exports"
SALES_DIR = DATA_DIR / "sales"          # per-sale folders live here
BACKUP_DIR = DATA_DIR / "backups"       # nightly database backups
DB_PATH = DATA_DIR / "abbey.db"
CONFIG_JSON = BASE_DIR / "abbey_config.json"

for _d in (DATA_DIR, PHOTO_DIR, EXPORT_DIR, SALES_DIR, BACKUP_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Go Auction picklists (from the Appraisal Import template, Sheet2) ---
GO_CATEGORIES = [
    "Antique implements & practical items", "Artwork & Decorative Items",
    "Asian Decorative Arts", "Books & Ephemera", "Cameras & Photography",
    "Ceramics, Porcelain & Glass", "Clocks & Scientific Equipments",
    "Clothing & Accessories", "Coins & Banknotes", "Collectables, Toys, Cards & Misc",
    "Electrical - Computers, Hifi, Audio", "Electrical, Appliances & Lighting",
    "Furniture - Antique", "Furniture - Modern", "Jewellery & Watches",
    "Motor Vehicles", "Music & Multimedia", "Musical Instruments",
    "Outdoor, Garage & Garden", "Rugs & Carpets", "Silverware and metalware",
    "Sporting goods & Memorabilia", "Stamps", "Tools", "Weapons & Militaria",
    "Wines & Spirits", "Unclaimed Goods", "AA No Category set",
]
GO_LOCATIONS = [
    "", "Bathroom", "Bedroom 1", "Bedroom 2", "Bedroom 3", "Bedroom 4", "Dining Room",
    "Games Room", "Garage", "Garden", "Hallway", "Kitchen", "Laundry", "Living Room",
    "Other Room", "Outside", "Study", "TV Room",
]
GO_CONSIGN_TO = [
    "Weekly Estate", "Classics Collection", "Collector's Showcase",
    "Commercial / Off-Site Auction", "Fine Jewellery - Special", "Modern Living",
    "Special Auction", "Unclaimed Finds", "Wine - Special",
]
GO_VALUERS = ["", "David Smith", "Oliver Harling", "Tom Farrelly", "Hugh Farrelly"]
GO_RESERVE_RULES = ["", "NCV", "Donated", "Not Taken"]
GO_CONDITIONS = ["", "NCV", "Donated", "Not Taken"]


# ---------------------------------------------------------------------------
# Main settings object
# ---------------------------------------------------------------------------
@dataclass
class Settings:
    # --- House identity ---
    house_name: str = "Abbeys Auctions"
    house_location: str = "Burwood, Melbourne VIC"
    buyers_premium_pct: float = 24.0
    currency: str = "AUD"

    # --- Claude / model ---
    model_primary: str = "claude-fable-5"         # identify + draft + research (most capable)
    model_cheap: str = "claude-haiku-4-5-20251001"  # fast checks: photo angles, quick edits
    max_tokens: int = 1200
    enable_web_research: bool = True   # let Abbey consult the web for unknown items

    # --- Voice conversation (Sweep 1) ---
    # Speech-to-text so staff can TALK to Abbey about the item, hands-free-ish.
    stt_provider: str = "deepgram"     # cloud STT, lowest latency
    stt_model: str = "nova-2"
    stt_sample_rate: int = 16000
    stt_block_ms: int = 30             # mic block size for silence detection
    stt_threshold_rms: float = 500.0   # loudness above this = speech (int16 scale)
    stt_silence_ms: int = 800          # stop after this much trailing quiet
    stt_max_seconds: int = 12          # hard cap on one utterance
    converse_max_tokens: int = 400     # Abbey's spoken replies are short
    research_max_tokens: int = 1600    # "show me information" web research

    # --- Voice output (Sweep 4): instant cloud voice with offline fallback ---
    tts_provider: str = "aura"         # "aura" (cloud, natural) | "offline"
    aura_model: str = "aura-asteria-en"   # natural female voice
    tts_sample_rate: int = 24000

    # --- Hands-free conversation (Sweep 4) ---
    handsfree_require_wake: bool = False   # False = talk any time; True = say "Abbey" first
    handsfree_barge_in: bool = True        # start talking to interrupt her
    handsfree_poll_seconds: float = 0.4    # how often the UI drains heard utterances

    # --- "Look closer" detailed tiled analysis ---
    detail_grid: int = 2               # split the photo into grid×grid tiles
    detail_upscale: float = 2.0        # zoom each tile this much
    detail_max_tokens: int = 900
    low_confidence_threshold: float = 0.45   # below this, Abbey suggests a closer look

    # --- Reliability ---
    retry_attempts: int = 3            # retry transient API failures this many times
    retry_base_delay: float = 0.6      # seconds, doubles each retry
    backup_keep: int = 14              # nightly DB backups to keep
    backup_min_interval_hours: float = 20.0  # ~once a day
    auto_research: bool = True         # start researching in the background after capture

    # --- Knowledge matrix + correlations ---
    weight_staff_live: float = 3.0     # a person watching the item outranks history
    weight_staff_rule: float = 3.0     # a taught heuristic
    weight_sold_data: float = 1.0      # learned from past hammers
    weight_flag: float = 2.0           # a trend Abbey spotted herself
    corr_min_samples: int = 3          # need this many recent lots to call a trend
    corr_threshold: float = 0.15       # >15% consistent move = a trend
    corr_lookback: int = 200

    # --- Source snapshots (Sweep 3) ---
    snapshot_width: int = 1024
    snapshot_timeout: int = 30
    snapshot_limit: int = 6            # max source pages to screenshot per lot

    # --- Learning from uploaded sold-hammer data (the moat) ---
    learn_low_pct: float = 25.0        # band = 25th..75th percentile of real hammers
    learn_high_pct: float = 75.0
    learn_min_samples: int = 3         # need this many sold lots in a category to trust it

    # --- Startup ---
    start_screen: str = "home"         # "home" | "catalogue"

    # --- Camera ---
    camera_index: int = 0              # 0 = first USB camera
    frame_width: int = 1920
    frame_height: int = 1080
    burst_count: int = 24              # how many frames to grab per "capture"
    burst_seconds: float = 5.0         # spread the burst over this long (gives time to spin item)
    # Multi-angle capture gate (looser = accepts busier backgrounds)
    angle_min_score: float = 0.35      # accept a frame at/above this composition score
    angle_reject_clutter: bool = False # if True, reject 'excessive' background clutter
    angle_burst_frames: int = 6        # frames grabbed per "Capture angle" press; sharpest is used
    keep_best_n: int = 4               # show this many candidate photos to choose from
    motion_still_threshold: float = 0.012   # below this mean-diff = item is "still"
    motion_active_threshold: float = 0.05   # above this = item is being moved/presented
    auto_capture: bool = False         # if True, fire a burst automatically once item settles

    # --- Frame-quality weighting (must roughly sum to 1.0) ---
    w_sharpness: float = 0.45
    w_exposure: float = 0.25
    w_contrast: float = 0.15
    w_subject: float = 0.15

    # --- Learning engine ---
    # A per-category price adjustment smaller than this (e.g. 8%) is applied
    # automatically. Anything larger is a "learning shift" that must be approved
    # with the passcode.
    auto_learn_band_pct: float = 8.0
    min_samples_to_learn: int = 4      # need this many corrections before proposing a shift
    correction_lookback: int = 40      # only consider this many recent corrections per category
    source_min_uses_to_judge: int = 5  # need this many uses before demoting a source
    source_bad_rate_to_demote: float = 0.5  # >50% bad outcomes => propose demotion

    # --- Security ---
    max_passcode_attempts: int = 5     # lock the learning/settings panel after this many fails
    lockout_seconds: int = 300

    # --- Voice ("Abbey") ---
    voice_enabled: bool = True
    voice_name_hint: str = "female"    # picked from installed system voices
    voice_rate: int = 172              # words per minute
    voice_volume: float = 0.9

    # --- Cloud upload (optional) ---
    # "none" | "local_folder" | "s3" | "gdrive"
    upload_mode: str = "none"
    upload_local_folder: str = ""      # e.g. a mapped NAS drive
    s3_bucket: str = ""
    s3_prefix: str = ""                # optional key prefix, e.g. "abbeys/photos"
    s3_region: str = ""                # e.g. "ap-southeast-2" (Sydney)

    # --- Go Auction / Bidpath CSV export ---
    # These are the exact columns of the Go Auction "Appraisal Import" template.
    csv_columns: list = field(default_factory=lambda: [
        "Line Number", "Title", "Description", "Low Estimate", "High Estimate",
        "Categories", "Location", "Consign To", "Valuer", "Reserve Rule",
        "Reserve Price", "Condition",
    ])
    # Defaults applied to every lot unless staff change them
    default_consign_to: str = "Weekly Estate"
    default_valuer: str = ""
    default_location: str = ""
    default_reserve_rule: str = ""

    def save(self, path: Path = CONFIG_JSON) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: Path = CONFIG_JSON) -> "Settings":
        s = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                for k, v in data.items():
                    if hasattr(s, k):
                        setattr(s, k, v)
            except (json.JSONDecodeError, OSError):
                pass  # fall back to defaults on a corrupt file
        return s


def get_api_key() -> str | None:
    """Anthropic key comes from the environment, never from a file in the repo."""
    return os.environ.get("ANTHROPIC_API_KEY")


def get_stt_key() -> str | None:
    """Deepgram (speech-to-text) key, from the environment."""
    return os.environ.get("DEEPGRAM_API_KEY")


SETTINGS = Settings.load()
