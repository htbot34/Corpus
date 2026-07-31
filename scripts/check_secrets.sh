#!/usr/bin/env bash
# Fail if anything that could be a credential is committable.
#
# Run directly, or via `pytest tests/test_secrets.py` which wires it into the
# suite so a leaked key is a red build rather than something noticed later in a
# pasted log.
#
#   scripts/check_secrets.sh          # scan, exit 1 on any finding
#   scripts/check_secrets.sh --list   # also print what was scanned
#
# Scope is deliberately "what git would take": tracked files plus untracked
# files that are not ignored. Ignored paths (.env, out/, captures/, .venv/) are
# where secrets are *supposed* to live and scanning them would guarantee a
# false positive on every run, which is how a check gets disabled.
#
# NOTE ON SELF-MATCHING: the key prefixes below are written as two adjacent
# shell string literals ('sk-''ant-'). The shell concatenates them into the real
# pattern at runtime, but the literal prefix never appears in this file, so the
# scanner does not flag itself. Do not "tidy" these into single quotes.

set -euo pipefail

cd "$(dirname "$0")/.."

RED=''; GREEN=''; BOLD=''; OFF=''
if [ -t 1 ]; then RED=$'\033[31m'; GREEN=$'\033[32m'; BOLD=$'\033[1m'; OFF=$'\033[0m'; fi

findings=0
report() {
  findings=$((findings + 1))
  printf '%s%sLEAK%s %s\n' "$BOLD" "$RED" "$OFF" "$1"
}

# --- patterns -------------------------------------------------------------
# Anthropic keys.
PAT_ANTHROPIC='sk-''ant-[A-Za-z0-9_-]{8,}'
# twitterapi.io keys.
PAT_TWITTERAPI='new''1_[A-Za-z0-9]{8,}'
# A secret env var assigned something that is not an obvious placeholder.
PAT_ASSIGNED='(X_API_KEY|ANTHROPIC_API_KEY|APIDANCE_KEY|SOCIALDATA_KEY)[[:space:]]*=[[:space:]]*['"'"'"]?[A-Za-z0-9_/+=.-]{16,}'
# Generic high-entropy run: 32+ chars of key alphabet containing lower, upper,
# and digit. Pure-digit (tweet ids) and lowercase-hex (git shas, sha256 sums)
# runs fail the mixed-case test and are therefore not reported.
PAT_ENTROPY='[A-Za-z0-9_+/=-]{32,}'

# Files exempted from the entropy heuristic only. They still get the exact
# prefix checks above.
#   uv.lock          - dependency hashes, high entropy by construction
#   *.lock/*.sum     - same, for any lockfile added later
entropy_exempt() {
  case "$1" in
    uv.lock|*.lock|*.sum|*.min.js|*.svg) return 0 ;;
    *) return 1 ;;
  esac
}

# A line carrying this marker is skipped by the entropy heuristic. For test
# vectors and documented examples — never for a real credential.
ALLOW_MARKER='secrets-scan: ok'

# --- file list ------------------------------------------------------------
files=$( { git ls-files; git ls-files --others --exclude-standard; } | sort -u )
if [ "${1:-}" = "--list" ]; then
  printf 'scanning %s files:\n' "$(printf '%s\n' "$files" | grep -c . || true)"
  printf '%s\n' "$files" | sed 's/^/  /'
  echo
fi

# Documented placeholders. Excuses a PAT_ASSIGNED hit only — a real key prefix
# is never a placeholder no matter what the surrounding line says.
PAT_PLACEHOLDER='your_|_here|YOUR_|xxxx|XXXX|placeholder|<[a-zA-Z_]+>|\.\.\.'

# --- 1. hard prefix matches ----------------------------------------------
for pat_name in ANTHROPIC TWITTERAPI ASSIGNED; do
  eval "pat=\$PAT_$pat_name"
  while IFS= read -r f; do
    [ -f "$f" ] || continue
    # -I skips binary files; -n gives line numbers for a usable report.
    if hits=$(grep -InE "$pat" -- "$f" 2>/dev/null); then
      while IFS= read -r hit; do
        if [ "$pat_name" = ASSIGNED ]; then
          case "$hit" in
            *"$ALLOW_MARKER"*) continue ;;
          esac
          if printf '%s' "$hit" | grep -qE "$PAT_PLACEHOLDER"; then continue; fi
        fi
        report "$f:${hit%%:*} matches $pat_name key pattern"
      done <<< "$hits"
    fi
  done <<< "$files"
done

# --- 2. generic high entropy ---------------------------------------------
while IFS= read -r f; do
  [ -f "$f" ] || continue
  entropy_exempt "$f" && continue
  hits=$(
    grep -Iv "$ALLOW_MARKER" -- "$f" 2>/dev/null \
      | grep -oE "$PAT_ENTROPY" 2>/dev/null \
      | grep '[a-z]' | grep '[A-Z]' | grep '[0-9]' || true
  )
  [ -n "$hits" ] || continue
  while IFS= read -r tok; do
    [ -n "$tok" ] || continue
    report "$f: high-entropy string '${tok:0:12}...' (${#tok} chars). If it is not a \
secret, append '$ALLOW_MARKER' to that line."
  done <<< "$hits"
done <<< "$files"

# --- 3. .env hygiene ------------------------------------------------------
if ! git check-ignore -q .env 2>/dev/null; then
  report ".env is not gitignored"
fi
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  report ".env is TRACKED by git"
fi
if [ -n "$(git log --all --full-history --oneline -- .env 2>/dev/null)" ]; then
  report ".env appears in git history — the key must be rotated, not just removed"
fi

# --- 4. .env.example must hold placeholders only --------------------------
if [ -f .env.example ]; then
  while IFS= read -r line; do
    case "$line" in
      \#*|'') continue ;;
    esac
    name=${line%%=*}
    value=${line#*=}
    case "$name" in
      *KEY|*TOKEN|*SECRET|*PASSWORD) ;;
      *) continue ;;
    esac
    # Placeholders are expected to say so. Anything else is suspicious by length.
    case "$value" in
      *your_*|*_here|*YOUR_*|*xxx*|*XXX*|*placeholder*|'') continue ;;
    esac
    if [ ${#value} -ge 16 ]; then
      report ".env.example: $name looks like a real value, not a placeholder"
    fi
  done < .env.example
fi

# --- verdict --------------------------------------------------------------
echo
if [ "$findings" -gt 0 ]; then
  printf '%s%s%d finding(s). Nothing here should be committed.%s\n' "$BOLD" "$RED" "$findings" "$OFF"
  echo 'If a finding is a false positive, add the marker described above rather'
  echo 'than widening a pattern — a pattern widened once stays widened.'
  exit 1
fi
printf '%s%sclean%s — no credential material in tracked or committable files\n' "$BOLD" "$GREEN" "$OFF"
