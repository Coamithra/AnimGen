"""Seed a starter project for the BIKER character from its keyposes.

Builds data/Biker.animproj with a full SF2-style move set authored as Shots. This is the
ROUGH/STARTER pass: EVERYTHING runs on the local Wan 2.2 14B backend ($0, overnight on the
GPU), so the whole set can be generated unattended in one batch. Individual shots can be
upgraded to hosted hero models later (the `ml` column records the suggested online model
per move - Seedance for ground actions, Vidu for airborne singles - per the Fighter
CHARACTER_PIPELINE/MOVELIST division of labour).

Local Wan picks its workflow from the keyframes:
  - pose-to-pose TRANSITIONS (end keyframe set) -> FLF first-last tween.
  - prompt-driven ACTIONS (no end keyframe)     -> open-ended i2v continuation
    (the end-image conditioning is severed by prepare_workflow).

CAMERA (follow-cam A/B, from the plague-doctor experiment - PD_SHOTBOARD.md
`overnight_pd_tweaks.json` + scripts/make_pd_move_tweaks.py):
  - TRAVEL / airborne moves (walk, jumps, air attacks, air recovery, knockdown) use
    FOLLOW-CAM: a SQUARE canvas + a prompt tail asking the camera to stay locked on the
    character at constant scale, and a negative that DROPS "camera pan"/"tracking shot"
    (we want those) but KEEPS "camera zoom"/"dolly" (scale drift wrecks frame extraction).
    The world travel is re-added in game code, so every pixel is spent on the character
    (bigger sprite = the "higher resolution on the character" win).
  - everything else stays FIXED-cam (the character doesn't travel, so a still camera +
    full negative is correct).
We adopt the load-bearing follow-cam mechanics (camera lock + constant scale + magenta +
negative tweak + square canvas) but NOT the PD-specific cel-shaded art-style words, so the
biker's own look isn't restyled. Add character art direction to the tails if wanted.

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/seed_biker.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402
from pipeline import framing  # noqa: E402
from store.project import Project  # noqa: E402

KEYFRAMES = paths.FIGHTER_ROOT / "assets" / "biker" / "keyframes"
OUT = paths.DATA_DIR / "Biker.animproj"

# pose key -> source filename in the keyframes dir
KEYPOSES = {
    "idle": "idle.png", "crouch": "crouch.png", "block": "block.png",
    "crouch_block": "crouch block.png", "crouch_hurt": "crouch hurt.png",
    "high_hurt": "high hurt.png", "hurt_mid": "hurt mid.png", "jump": "jump.png",
    "victory": "victory.png", "air_hit": "air hit.png",
}

# pose key -> fixed-cam framing category (follow-cam shots use FOLLOW_PLACE instead)
CATEGORY = {
    "idle": "stand", "block": "stand", "high_hurt": "stand", "hurt_mid": "stand",
    "victory": "stand", "crouch": "crouch", "crouch_block": "crouch",
    "crouch_hurt": "crouch", "jump": "air", "air_hit": "air",
}

# fixed-cam placements {scale (sprite-h / canvas-h), cx, cy}: grounded poses share a ground
# line at ~0.90 (cy = 0.90 - scale/2); airborne sit mid-canvas.
PLACEMENT = {
    "stand": {"scale": 0.62, "cx": 0.5, "cy": 0.59},
    "crouch": {"scale": 0.42, "cx": 0.5, "cy": 0.69},
    "air": {"scale": 0.42, "cx": 0.5, "cy": 0.42},
}
# follow-cam: character centered and LARGE, same scale on both ends (constant-scale = the
# whole point of follow-cam; the camera holds the character in place).
FOLLOW_PLACE = {"scale": 0.60, "cx": 0.5, "cy": 0.50}

# --- camera prompt tails + negatives (from make_pd_move_tweaks.py) ---------------
TAIL_FOLLOW = (" The camera stays locked on the character, keeping him centered in frame "
               "at the same scale the entire time. Flat solid magenta background.")
TAIL_FIXED = (" Fixed camera, static shot, completely still camera. Flat solid magenta "
              "background.")
# follow: drop "camera pan"/"tracking shot" (we WANT those); keep "camera zoom, dolly"
# (scale changes ruin extraction) + "scene/background change" (magenta must hold).
NEG_FOLLOW = ("camera zoom, dolly, scene change, background change, new background, "
              "realistic, photorealistic, 3D render, motion blur, blurry, extra limbs, "
              "deformed hands, text, watermark, frozen, still image, static pose")
NEG_FOLLOW_WALK = NEG_FOLLOW + ", attacking, kicking, punching, standing still, motionless"
NEG_FIXED = ("camera pan, camera zoom, dolly, tracking shot, scene change, background "
             "change, new background, realistic, photorealistic, 3D render, motion blur, "
             "blurry, extra limbs, deformed hands, text, watermark, frozen, still image, "
             "static pose")

LOCAL_MODEL = "local-flf-wan14b"
# `ml` (per move below) = the SUGGESTED hosted model to upgrade that shot to later.
SUGGESTED_ONLINE = {"L": "local-flf-wan14b", "S": "seedance-2.0-std", "V": "vidu-q3-pro"}

# Personality: he's a "rock-and-roll" biker. This LEAD anchors his identity on every shot;
# the start keyframe holds his APPEARANCE, so the prompt's rocker flavour mostly drives wild,
# exaggerated MOTION. For FLF shots the flair stays manner-of-motion (the end pose is pinned).
LEAD = "A 2D cel-shaded fighting game character, a wild rock-and-roll biker, "

# (name, start, end|"", cam, aspect, length, ml, prompt-without-LEAD/camera/bg-clause)
# follow-cam (travel/airborne) shots are square 1:1 so all pixels go to the character.
MOVES = [
    # --- movement ---
    ("idle", "idle", "", "fixed", "1:1", 33, "S",
     "stays loose in a cocky fighting stance, bobbing his head and shoulders to a beat with restless rocker swagger, shifting his weight, never standing still. He does not step or attack."),
    ("walk_forward", "idle", "", "follow", "1:1", 49, "S",
     "swaggers forward in his fighting stance with a cocky rock-and-roll strut, fists up in guard, bouncing to a beat and leaning into each step, without stopping, at a constant pace. He keeps strutting forward continuously the entire time."),
    ("walk_backward", "idle", "", "follow", "1:1", 49, "S",
     "struts backward in his fighting stance with a cocky rocker bounce, facing forward, fists up in guard, without stopping, at a constant pace. He keeps backpedaling continuously the entire time."),
    ("crouch_down", "idle", "crouch", "fixed", "1:1", 17, "L",
     "drops into a low crouched guard with a sharp rocker snap in one smooth motion, then holds the crouch."),
    ("crouch_up", "crouch", "idle", "fixed", "1:1", 17, "L",
     "springs up from the crouch back to his cocky standing stance in one smooth motion, then holds."),
    ("jump_vertical", "idle", "idle", "follow", "1:1", 33, "V",
     "explodes straight up high into the air with wild rock-and-roll energy, kicking his legs up at the peak like a stage leap, then drops back down and lands on his feet in his fighting stance. One single vertical jump, straight up and back down."),
    ("jump_forward", "idle", "idle", "follow", "1:1", 33, "V",
     "launches up and forward in one big wild rock-and-roll leap, legs tucked at the peak, then lands on his feet in his fighting stance. One single forward jump."),
    # --- standing attacks ---
    ("punch_light", "idle", "", "fixed", "1:1", 33, "S",
     "throws one quick snappy jab with his lead hand and cocky rocker flair, then snaps back to his stance."),
    ("punch_heavy", "idle", "", "fixed", "1:1", 33, "S",
     "winds up and unloads one crazy rock-and-roll haymaker with full wild commitment, then recovers to his stance."),
    ("kick_light", "idle", "", "fixed", "1:1", 33, "S",
     "throws one quick flashy rocker front kick, then returns to his stance."),
    ("kick_heavy", "idle", "", "fixed", "1:1", 33, "S",
     "performs one crazy rock-and-roll heavy forward kick with wild momentum, then recovers to his stance."),
    # --- crouching attacks ---
    ("crouch_punch_light", "crouch", "", "fixed", "1:1", 33, "S",
     "from a low crouch throws one quick snappy low jab with rocker flair, then settles back into the crouch."),
    ("crouch_punch_heavy", "crouch", "", "fixed", "1:1", 33, "S",
     "from a low crouch erupts into one wild rock-and-roll rising uppercut, then settles back into the crouch."),
    ("crouch_kick_light", "crouch", "", "fixed", "1:1", 33, "S",
     "from a low crouch throws one quick flashy low kick, then settles back into the crouch."),
    ("crouch_kick_sweep", "crouch", "", "fixed", "1:1", 33, "S",
     "from his low crouch rips one fierce spinning rock-and-roll leg sweep low along the ground, then returns to his crouched guard. He does not stand up."),
    # --- air attacks (airborne -> follow-cam) ---
    ("air_punch", "jump", "", "follow", "1:1", 33, "V",
     "at the peak of his jump, still airborne, hammers down one wild rock-and-roll downward punch, entirely in the air before his feet touch the ground."),
    ("air_kick", "jump", "", "follow", "1:1", 33, "V",
     "at the peak of his jump, still airborne, throws one crazy rock-and-roll diving kick, entirely in the air before his feet touch the ground."),
    # --- defense (FLF held-pose tweens, fixed-cam) ---
    ("block_high", "idle", "block", "fixed", "1:1", 17, "L",
     "snaps both arms up into a defiant rocker standing block guard in one quick motion, then holds the block."),
    ("block_high_recover", "block", "idle", "fixed", "1:1", 17, "L",
     "drops his guard from the standing block back to his cocky stance, then holds."),
    ("block_crouch", "crouch", "crouch_block", "fixed", "1:1", 17, "L",
     "from the crouch snaps up a low rocker blocking guard in one quick motion, then holds the crouching block."),
    ("block_crouch_recover", "crouch_block", "crouch", "fixed", "1:1", 17, "L",
     "lowers the low guard back into the plain crouch, then holds."),
    # --- hurt (FLF held-pose tweens; recovery is its own render, never a reversed snap-in) ---
    ("hurt_high", "idle", "high_hurt", "fixed", "1:1", 17, "L",
     "snaps his head back hard as he is struck high, one wild recoil, then holds the staggered pose."),
    ("hurt_high_recover", "high_hurt", "idle", "fixed", "1:1", 17, "L",
     "shakes it off and recovers from the high hit back to his cocky stance, then holds."),
    ("hurt_mid", "idle", "hurt_mid", "fixed", "1:1", 17, "L",
     "doubles over as he is struck in the gut, one sharp recoil, then holds the hunched pose."),
    ("hurt_mid_recover", "hurt_mid", "idle", "fixed", "1:1", 17, "L",
     "straightens up and shakes off the gut hit back to his stance, then holds."),
    ("crouch_hurt", "crouch", "crouch_hurt", "fixed", "1:1", 17, "L",
     "while crouching he flinches hard from a hit, one sharp recoil, then holds the wince."),
    ("crouch_hurt_recover", "crouch_hurt", "crouch", "fixed", "1:1", 17, "L",
     "shrugs off the hit and recovers back into the plain crouch, then holds."),
    # --- knockdown / air hurt (travel -> follow-cam, end-pinned to the stance) ---
    ("air_recovery", "air_hit", "idle", "follow", "1:1", 33, "V",
     "tumbling through the air after a launch, flips over with wild rocker flair, rights himself in mid-air, and lands cleanly on his feet in his fighting stance. He stays airborne until the final landing. One single recovery flip."),
    ("knockdown", "air_hit", "idle", "follow", "1:1", 49, "S",
     "is launched into the air by a powerful hit: he tumbles backward through the air, crashes down hard onto his back, slides to a stop, lies sprawled for a beat, then kicks back up onto his feet into his cocky fighting stance. One continuous knockdown and get-up."),
    # --- special / presentation ---
    ("fireball", "idle", "", "fixed", "16:9", 33, "S",
     "thrusts both hands forward and unleashes one wild glowing rock-and-roll energy blast that screams forward, then strikes a cocky stance."),
    ("win_pose", "idle", "victory", "fixed", "1:1", 17, "L",
     "breaks into a triumphant rock-and-roll victory pose with wild rockstar swagger in one smooth motion, then holds the win pose."),
]


def main() -> None:
    missing = [f for f in KEYPOSES.values() if not (KEYFRAMES / f).exists()]
    if missing:
        raise SystemExit(f"missing keyposes in {KEYFRAMES}: {missing}")

    paths.ensure_dirs()
    if OUT.exists():
        raise SystemExit(f"{OUT} already exists - delete it (and Biker.assets) for a clean rebuild")

    project = Project.new("Biker")
    assets: dict[str, str] = {}  # pose key -> imported asset path (import each once)
    for key, fname in KEYPOSES.items():
        assets[key] = str(project.import_asset(KEYFRAMES / fname))

    follow_count = 0
    suggested: dict[str, int] = {}
    for name, start, end, cam, aspect, length, ml, prompt in MOVES:
        follow = cam == "follow"
        full_prompt = LEAD + prompt + (TAIL_FOLLOW if follow else TAIL_FIXED)
        if follow:
            neg = NEG_FOLLOW_WALK if name.startswith("walk") else NEG_FOLLOW
            start_place = dict(FOLLOW_PLACE)
            end_place = dict(FOLLOW_PLACE)        # constant scale on both ends
        else:
            neg = NEG_FIXED
            start_place = dict(PLACEMENT[CATEGORY[start]])
            end_place = dict(PLACEMENT[CATEGORY[end]]) if end else dict(start_place)

        w, h = framing.canvas_size(aspect, local=True)
        crop = {"aspect": aspect, "start": start_place, "end": end_place}
        project.add_shot(
            name, model_id=LOCAL_MODEL, settings={"seed": -1, "length": length},
            prompt=full_prompt, negative_prompt=neg,
            start_frame=assets[start], end_frame=assets[end] if end else None,
            canvas_w=w, canvas_h=h, crop=crop,
        )
        follow_count += follow
        suggested[SUGGESTED_ONLINE[ml]] = suggested.get(SUGGESTED_ONLINE[ml], 0) + 1

    project.save_as(OUT)
    shots = project.list_shots()
    flf = sum(1 for s in shots if s.end_frame)
    print(f"wrote {project.path} ({len(shots)} shots, all on {LOCAL_MODEL})")
    print(f"  {follow_count:2d}  follow-cam (square canvas, camera locked on character)")
    print(f"  {len(shots) - follow_count:2d}  fixed-cam")
    print(f"  {flf:2d}  FLF (end-pinned)   {len(shots) - flf:2d}  open-ended i2v")
    print("suggested hosted upgrade per shot (for later):")
    for m, n in sorted(suggested.items()):
        print(f"  {n:2d}  -> {m}")


if __name__ == "__main__":
    main()
