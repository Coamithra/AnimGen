"""App-global library of reusable prompt prefabs (positive + negative pairs).

Unlike a shot's own prompt (which lives in the .animproj), these templates are shared
across every project so reusable choreography phrasing / camera-lock terms can be saved
once and applied anywhere. Persisted to data/prompt_templates.json.

File shape: {"format": "animgen-prompt-templates", "version": 1,
             "templates": [{"name": <str>, "positive": <str>, "negative": <str>}, ...]}

Reads tolerate a missing/corrupt file (returns the seed templates). Writes go through the
project's atomic-write helper under a lock, mirroring store.schema_cache's discipline.
Paths are read from `paths` at call time so tests can override `paths.PROMPT_TEMPLATES`.
"""
from __future__ import annotations

import json
import threading
from typing import Optional

import paths
import library
from store.project import _atomic_write_json

_FORMAT = "animgen-prompt-templates"
_VERSION = 1
_lock = threading.RLock()

# Shipped starter prefabs, curated from the Fighter project's settled prompt log
# (../Fighter/PROMPTS.txt, sections 1-3 — the shipped move + transition prompts).
# Two generic scaffolds up top, then one template per distinct move/transition. Hosted
# (Seedance/Vidu) move prompts already bake their own style tail and get the authored
# default negative (those models ignore it, but supporting models get the baseline);
# local (Wan/FLF) prompts append the shared local style tail and the Section-6 negatives.
# Hosted/PD duplicates of the same move are collapsed (PD-unique moves — idle, walk back,
# dagger — are kept since the hosted set has no equivalent).

# Shared local style tail (PROMPTS.txt: "[STYLE TAIL - local]"). Appended to every
# local FLF/Wan prompt below so each template is a complete, usable prompt.
_LOCAL_TAIL = ("Fixed camera, static shot, completely still camera. Flat solid magenta "
               "background. Stylized cel-shaded 2D game art with clean bold outlines, "
               "snappy exaggerated fighting game motion.")

# Section-6 shared negatives.
_NEG_STD = ("camera pan, camera zoom, dolly, tracking shot, scene change, background "
            "change, new background, realistic, photorealistic, 3D render, motion blur, "
            "blurry, extra limbs, deformed hands, text, watermark, frozen, still image, "
            "static pose")
_NEG_IDLE_WALK = ("camera pan, camera zoom, dolly, tracking shot, scene change, background "
                  "change, walking, stepping forward, attacking, kicking, punching, "
                  "realistic, photorealistic, 3D render, blurry, extra limbs, deformed "
                  "hands, text, watermark, frozen, still image, static pose, motionless, "
                  "standing still")

_DEF_NEG = library.default_negative_prompt()


def _hosted(name: str, positive: str) -> dict:
    """A hosted (Seedance/Vidu) move template — full prompt, authored default negative."""
    return {"name": name, "positive": positive, "negative": _DEF_NEG}


def _local(name: str, body: str, negative: str = _NEG_STD) -> dict:
    """A local (FLF/Wan) template — body + shared local style tail + a Section-6 negative."""
    return {"name": name, "positive": body + " " + _LOCAL_TAIL, "negative": negative}


_SEEDS = [
    # --- Generic scaffolds (reusable across any new move) ---
    {"name": "Camera-locked action",
     "positive": "fixed camera, character performs the action in place, clean loop, "
                 "no camera movement, consistent background",
     "negative": _DEF_NEG},
    {"name": "Snappy impact",
     "positive": "sharp anticipation then explosive impact, strong weight transfer, "
                 "exaggerated keyframes, snappy timing, fixed camera",
     "negative": _DEF_NEG},

    # --- Section 1: hosted move prompts (Seedance / Vidu) ---
    _hosted("Punch combo (light + heavy)",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "fixed lens for the entire video. A 2D cel-shaded fighting game character "
            "performs two punches in sequence: first one fast light jab, his lead fist "
            "snapping straight forward at face height and snapping back, then he returns to "
            "his fighting stance, then one powerful heavy punch, his rear fist driving "
            "straight forward with his full body weight rotating behind it, then he returns "
            "to his fighting stance and holds it. He punches forward in the direction he is "
            "facing. Each punch is a single explosive movement with a clear pause in his "
            "stance between them. He does not kick, does not jump, does not step forward, "
            "does not walk. Fast, powerful, exaggerated cartoon fighting-game motion with "
            "snappy timing. Flat solid magenta background stays unchanged. Clean bold "
            "outlines, flat cel shading, 2D animation style."),
    _hosted("Kick - heavy forward",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "fixed lens for the entire video. A 2D cel-shaded fighting game character "
            "performs one fast forward kick, then returns to his fighting stance and holds "
            "it. Fast, powerful, exaggerated cartoon fighting-game motion with snappy "
            "timing. Flat solid magenta background stays unchanged. Clean bold outlines, "
            "flat cel shading, 2D animation style."),
    _hosted("Kick - high/low combo",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "fixed lens for the entire video. A 2D cel-shaded fighting game character "
            "performs two kicks in sequence: first one fast high forward kick, then he "
            "returns to his fighting stance, then one fast low forward kick aimed at shin "
            "height, then he returns to his fighting stance and holds it. Each kick is a "
            "single explosive movement with a clear pause in his stance between them. Fast, "
            "powerful, exaggerated cartoon fighting-game motion with snappy timing. Flat "
            "solid magenta background stays unchanged. Clean bold outlines, flat cel "
            "shading, 2D animation style."),
    _hosted("Crouch - light punch + kick",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "fixed lens for the entire video. A 2D cel-shaded fighting game character is in "
            "a low crouch, guard up. Staying in his crouch, he throws one fast light punch "
            "straight forward at waist height, then pauses in his crouched guard, then snaps "
            "one fast light kick straight forward along the ground, then settles back into "
            "his crouched guard and holds it, staying crouched. He stays low in his crouch "
            "the entire time and strikes forward in the direction he is facing. Each strike "
            "is a single explosive movement with a clear crouched pause between them. He "
            "does not stand up, does not jump, does not punch upward. Fast, powerful, "
            "exaggerated cartoon fighting-game motion with snappy timing. Flat solid magenta "
            "background stays unchanged. Clean bold outlines, flat cel shading, 2D animation "
            "style."),
    _hosted("Crouch - heavy uppercut + sweep",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "fixed lens for the entire video. A 2D cel-shaded fighting game character is in "
            "a low crouch, guard up. He throws a fierce uppercut, then returns to his "
            "crouched guard and pauses, then performs a horizontal leg sweep low along the "
            "ground, then returns to his crouched guard and holds it. Each attack is a "
            "single explosive movement with a clear pause between them. He strikes in the "
            "direction he is facing. Fast, powerful, exaggerated cartoon fighting-game "
            "motion with snappy timing. Flat solid magenta background stays unchanged. Clean "
            "bold outlines, flat cel shading, 2D animation style."),
    _hosted("Jump - vertical",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "no tilt, fixed lens for the entire video. A 2D cel-shaded fighting game "
            "character performs two vertical jumps in sequence: from his fighting stance he "
            "crouches for an instant, leaps powerfully straight up into the air, rising a "
            "full body height, legs tucking up beneath him at the peak, falls back down and "
            "lands on the exact same spot with knees bending briefly to absorb the landing, "
            "returns to his fighting stance for a short pause, then performs a second "
            "identical vertical jump, lands the same way, and returns to his fighting "
            "stance. He jumps straight up and lands in place both times. He does not move "
            "left or right, does not kick, does not punch. Each jump is quick and explosive "
            "with a clear stance pause between them. Fast, snappy, exaggerated fighting-game "
            "motion. Flat solid magenta background stays unchanged. Clean bold outlines, "
            "flat cel shading, 2D animation style."),
    _hosted("Jump - forward",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "no tilt, fixed lens for the entire video. A 2D cel-shaded fighting game "
            "character performs one forward jump: from his fighting stance he crouches for "
            "an instant, then leaps powerfully forward and upward in the direction he is "
            "facing, rising a full body height while traveling forward through the air, body "
            "tucked aggressively with knees pulled up tight, then descends and lands well "
            "ahead of his starting spot, knees bending briefly to absorb the landing, and "
            "returns to his fighting stance. One single jump in one high forward arc. He "
            "does not kick, does not punch, does not jump again, does not turn around. Fast, "
            "snappy, exaggerated fighting-game motion. Flat solid magenta background stays "
            "unchanged. Clean bold outlines, flat cel shading, 2D animation style."),
    _hosted("Air kick - flying kick from apex",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "no tilt, fixed lens for the entire video. A 2D cel-shaded fighting game "
            "character is in mid-air at the top of a forward jump, knees tucked. ACTION 1 "
            "(the first moment, very fast): while he is still in mid-air, falling, he "
            "thrusts one leg straight out in a flying kick - the kick happens entirely in "
            "the air, before his feet ever touch the ground. He strikes in mid-air, and only "
            "then do his feet land on the ground, knees bending briefly. ACTION 2 (the "
            "entire rest of the video): he stands calmly in his fighting stance and holds a "
            "relaxed idle - fists raised, subtle breathing, gentle weight shifts. He does "
            "not kick after landing, does not kick again, does not punch, does not jump. The "
            "mid-air kick and landing are explosive and brief; the idle is long and calm. "
            "Fast, snappy, exaggerated fighting-game motion. Flat solid magenta background "
            "stays unchanged. Clean bold outlines, flat cel shading, 2D animation style."),
    _hosted("Air punch - flying punch from apex",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "no tilt, fixed lens for the entire video. A 2D cel-shaded fighting game "
            "character is in mid-air at the top of a forward jump, knees tucked. ACTION 1 "
            "(the first moment, very fast): while he is still in mid-air, falling, he drives "
            "one fist straight forward in a powerful flying punch - the punch happens "
            "entirely in the air, before his feet ever touch the ground. He strikes in "
            "mid-air, and only then do his feet land on the ground, knees bending briefly. "
            "ACTION 2 (the entire rest of the video): he stands calmly in his fighting "
            "stance and holds a relaxed idle - fists raised, subtle breathing, gentle weight "
            "shifts. He does not punch after landing, does not punch again, does not kick, "
            "does not jump. The mid-air punch and landing are explosive and brief; the idle "
            "is long and calm. Fast, snappy, exaggerated fighting-game motion. Flat solid "
            "magenta background stays unchanged. Clean bold outlines, flat cel shading, 2D "
            "animation style."),
    _hosted("Knockdown - fall and get up",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "fixed lens for the entire video. A 2D cel-shaded fighting game character has "
            "been knocked into the air and is falling backward. He crashes down flat on his "
            "back on the ground, bounces slightly from the impact, lies flat and still for a "
            "moment, then rolls over and pushes himself back up onto his feet, returning to "
            "his fighting stance and holding it. One continuous motion: fall, land hard, lie "
            "still, get up. He does not attack, does not jump, and stays facing the same "
            "direction the whole time. Flat solid magenta background stays unchanged. Clean "
            "bold outlines, flat cel shading, 2D animation style."),
    _hosted("Air recovery - mid-air backflip",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "fixed lens for the entire video. A 2D cel-shaded fighting game character has "
            "been knocked into the air and is falling backward. He recovers in mid-air: he "
            "twists and backflips while still airborne, righting himself completely before "
            "his feet ever touch the ground, and lands cleanly on his feet, dropping "
            "straight into his fighting stance and holding it. The flip happens entirely in "
            "the air. He does not crash, does not land on his back, does not lie down, does "
            "not attack. Fast, agile, acrobatic recovery with snappy timing. Flat solid "
            "magenta background stays unchanged. Clean bold outlines, flat cel shading, 2D "
            "animation style."),
    _hosted("Fireball",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "fixed lens for the entire video. A 2D cel-shaded fighting game character throws "
            "a fireball: he gathers glowing energy between his cupped hands, then thrusts "
            "both palms forward and launches a bright fireball projectile that flies "
            "horizontally all the way across the stage at chest height, leaving the frame. "
            "After throwing he returns to his fighting stance and holds it. He stays in "
            "place the entire time: does not walk, does not jump, does not follow the "
            "fireball. One powerful fluid throwing motion with snappy timing. Flat solid "
            "magenta background stays unchanged. Clean bold outlines, flat cel shading, 2D "
            "animation style."),
    _hosted("Walk - forward",
            "Static camera. The camera is completely stable, no movement, no zoom, no pan, "
            "fixed lens for the entire video. A cool 2D cel-shaded fighting game character "
            "walks forward across the stage, stride after stride, walking continuously for "
            "the entire video without stopping. He faces the same direction the entire video "
            "and never turns around. Exaggerated fighting game movement, full of "
            "personality. He does not attack, does not run, does not jump, does not turn "
            "around, does not stop walking. Flat solid magenta background stays unchanged. "
            "Clean bold outlines, flat cel shading, 2D animation style."),

    # --- Section 2: local FLF transition tweens (end-pinned keypose pairs) ---
    _local("Stand -> Crouch",
           "A 2D fighting game character drops from his standing fighting stance straight "
           "down into a deep low crouch, bending his knees and sinking down, keeping his "
           "guard up and his feet planted in place. One smooth quick crouching motion, then "
           "he holds the crouch. He does not jump, does not attack, does not step, does not "
           "turn."),
    _local("Crouch -> Stand",
           "A 2D fighting game character rises from his deep low crouch back up into his "
           "standing fighting stance, straightening his legs and keeping his guard up, feet "
           "planted in place. One smooth quick rising motion, then he holds his stance. He "
           "does not jump, does not attack, does not step, does not turn."),
    _local("Stand -> Hurt (high)",
           "A 2D fighting game character takes a hard punch to the face: his head snaps back "
           "and his body recoils into a staggered hurt pose, arm flung back, reeling from "
           "the hit. One sharp fast recoil, then he holds the staggered pose. He does not "
           "fall down, does not attack, does not step, does not turn."),
    _local("Hurt (high) -> Stand",
           "A 2D fighting game character is reeling in a staggered hurt pose, head thrown "
           "back from a hit he just took. He recovers: steadies himself, shakes it off and "
           "pulls himself back up into his fighting stance, guard returning up, then holds "
           "his stance. A deliberate, controlled recovery. He does not fall down, does not "
           "attack, does not step, does not turn."),
    _local("Stand -> Hurt (mid)",
           "A 2D fighting game character takes a hard punch to the stomach: he doubles over "
           "clutching his gut, knees buckling slightly, wincing from the hit. One sharp fast "
           "recoil, then he holds the doubled-over pose. He does not fall down, does not "
           "attack, does not step, does not turn."),
    _local("Hurt (mid) -> Stand",
           "A 2D fighting game character is doubled over clutching his stomach from a hit he "
           "just took. He recovers: straightens back up, shakes it off and returns to his "
           "fighting stance, guard coming back up, then holds his stance. A deliberate, "
           "controlled recovery. He does not fall down, does not attack, does not step, does "
           "not turn."),
    _local("Crouch -> Hurt (crouch)",
           "A 2D fighting game character in a low crouch, guard up, takes a hard hit while "
           "crouching: he flinches and doubles over in his crouch, wincing from the blow. "
           "One sharp fast recoil staying low to the ground, then he holds the hurt "
           "crouching pose. He does not stand up, does not fall over, does not attack, does "
           "not step."),
    _local("Hurt (crouch) -> Crouch",
           "A 2D fighting game character is wincing in a low crouch, doubled over from a hit "
           "he just took. He recovers: steadies himself and returns to his crouched guard "
           "position, fists back up, staying low the whole time. A deliberate, controlled "
           "recovery without standing up. He does not stand up, does not fall over, does not "
           "attack, does not step."),
    _local("Stand -> Block",
           "A 2D fighting game character snaps his guard up into a tight defensive block, "
           "raising his arms to shield his face and chest, bracing for an incoming attack. "
           "One sharp fast defensive motion, then he holds the block. He does not attack, "
           "does not step, does not crouch, does not turn."),
    _local("Block -> Stand",
           "A 2D fighting game character is braced in a tight defensive block, arms "
           "shielding his face. He relaxes the block and returns to his fighting stance, "
           "fists coming back to guard position. A quick controlled release. He does not "
           "attack, does not step, does not crouch, does not turn."),
    _local("Crouch -> Block (crouch)",
           "A 2D fighting game character in a low crouch snaps his arms up into a tight "
           "crouching block, shielding himself while staying low, bracing for an incoming "
           "attack. One sharp fast defensive motion staying crouched, then he holds the "
           "crouching block. He does not stand up, does not attack, does not step, does not "
           "turn."),
    _local("Block (crouch) -> Crouch",
           "A 2D fighting game character is braced in a low crouching block. He relaxes the "
           "block and returns to his crouched guard position, fists coming back down, "
           "staying low the whole time. A quick controlled release. He does not stand up, "
           "does not attack, does not step, does not turn."),
    _local("Stand -> Win pose",
           "A 2D fighting game character has just won the fight: from his fighting stance he "
           "thrusts his fist high into the air in a triumphant victory pose, chest out, "
           "celebrating his win, then holds the victory pose. One confident celebratory "
           "motion. He does not jump, does not walk, does not turn around."),

    # --- Section 3: Plague Doctor moves unique to the local set (no hosted equivalent) ---
    _local("Idle - breathing loop",
           "A 2D fighting game character breathes in his fighting stance: chest rising and "
           "falling, shoulders bobbing gently, fists making small circular motions in "
           "guard, his tattered cloak and the feather on his hat fluttering continuously, "
           "scarf swaying. Constant subtle motion in every frame, he never stops moving, "
           "weight shifting rhythmically between his feet. He stays in place in his stance.",
           _NEG_IDLE_WALK),
    _local("Walk - backward",
           "A 2D fighting game character walks backward toward the left with smooth steady "
           "backpedaling strides, stride after stride, without stopping, at a constant pace, "
           "facing toward the right the whole time. He keeps backpedaling the entire time.",
           _NEG_IDLE_WALK),
    _local("Dagger slash",
           "A 2D fighting game character draws a dagger from his belt and slashes it once "
           "through the air in a quick fierce arc, then sheathes it and returns to his "
           "fighting stance. One single fast slash. He does not step, does not kick, does "
           "not turn."),
]


def _normalize(t: dict) -> dict:
    return {"name": str(t.get("name", "")).strip(),
            "positive": str(t.get("positive", "")),
            "negative": str(t.get("negative", ""))}


def _load_list() -> list[dict]:
    try:
        with open(paths.PROMPT_TEMPLATES, encoding="utf-8") as f:
            doc = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return [dict(t) for t in _SEEDS]
    items = doc.get("templates") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return [dict(t) for t in _SEEDS]
    return [_normalize(t) for t in items if isinstance(t, dict) and str(t.get("name", "")).strip()]


def _write(items: list[dict]) -> None:
    _atomic_write_json(paths.PROMPT_TEMPLATES,
                       {"format": _FORMAT, "version": _VERSION, "templates": items})


def all_templates() -> list[dict]:
    """Every template, name-sorted (a copy; safe to read freely)."""
    with _lock:
        return sorted(_load_list(), key=lambda t: t["name"].lower())


def get(name: str) -> Optional[dict]:
    """The template with this name (exact match), or None."""
    with _lock:
        for t in _load_list():
            if t["name"] == name:
                return t
    return None


def save(name: str, positive: str, negative: str) -> dict:
    """Add or overwrite (by name) a template and persist immediately. Returns the entry."""
    rec = _normalize({"name": name, "positive": positive, "negative": negative})
    if not rec["name"]:
        raise ValueError("Template name is required")
    with _lock:
        items = [t for t in _load_list() if t["name"] != rec["name"]]
        items.append(rec)
        _write(items)
    return rec


def delete(name: str) -> bool:
    """Remove a template by name. Returns True if one was removed."""
    with _lock:
        items = _load_list()
        kept = [t for t in items if t["name"] != name]
        if len(kept) == len(items):
            return False
        _write(kept)
    return True
