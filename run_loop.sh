#!/usr/bin/env bash
# تشغيل حي: جدولة GitHub تُسقط أغلب التشغيلات (4-7 يوميًا بدل 48)، فكل تشغيل يبقى حيًا ويحدّث المنصة بنفسه
# عند الدقيقة 07 و37 من كل ساعة حتى LOOP_MINUTES، ثم يأتي التشغيل المجدول التالي فيكمل.
# متوقف من السبت 06:00 حتى الإثنين 00:00 بتوقيت فيينا (بعد التقاط إغلاق الجمعة وحيازات صناديقها).
# التشغيل اليدوي (Run workflow) لا يوقف الآلي: التشغيل الحي يلاحظه خلال دقيقة ويسلّمه الدور بعد آخر تحديث مكتمل،
# فيحدّث اليدوي فورًا ثم يستمر هو آليًا. AI_FORCE وFULL_REFRESH للتحديث الأول فقط.
set -u
git config user.name "gold-data-bot"
git config user.email "gold-data-bot@users.noreply.github.com"
BRANCH="${GITHUB_REF_NAME:-main}"
LIMIT=$(( ${LOOP_MINUTES:-0} * 60 ))
START=$(date +%s)
FIRST=1

manual_waiting() {
  [ "${EVENT:-}" = "" ] && return 1
  local n
  n=$(gh run list --workflow update.yml --branch "$BRANCH" --event workflow_dispatch --limit 10 \
        --json status -q '[.[] | select(.status=="pending" or .status=="queued" or .status=="waiting")] | length' 2>/dev/null)
  [ "${n:-0}" -gt 0 ]
}

weekend() {
  [ "${IGNORE_WEEKEND:-false}" = "true" ] && return 1
  local dow hour
  dow=$(TZ=Europe/Vienna date +%u)
  hour=$(TZ=Europe/Vienna date +%-H)
  [ "$dow" -eq 7 ] || { [ "$dow" -eq 6 ] && [ "$hour" -ge 6 ]; }
}

iteration() {
  git pull -q --rebase origin "$BRANCH" || git rebase --abort 2>/dev/null
  python fetch_gold.py || echo "fetch_gold.py exited with an error"
  if [ "${DRY_RUN:-false}" = "true" ]; then
    git checkout -q -- . && git clean -fdq -e __pycache__
    return
  fi
  git add -A
  git diff --cached --quiet && return
  git commit -qm "تحديث بيانات الذهب $(date -u +%F)"
  for i in 1 2 3; do
    git push -q origin "HEAD:$BRANCH" && return
    git pull -q --rebase origin "$BRANCH" || { git rebase --abort 2>/dev/null; break; }
  done
  echo "push failed; changes stay for the next iteration"
}

while :; do
  if weekend && ! { [ "$FIRST" = 1 ] && [ "${EVENT:-}" = "workflow_dispatch" ]; }; then
    echo "[loop] weekend (Vienna): platform paused"
    break
  fi
  echo "[loop] iteration at $(date -u +%H:%M:%S) UTC"
  iteration
  FIRST=0
  export AI_FORCE=false FULL_REFRESH=false     # الطلب اليدوي للتحديث الأول فقط
  [ "$LIMIT" -le 0 ] && break
  NOW=$(date +%s)
  NEXT=$(( NOW / 1800 * 1800 + 420 ))            # الدقيقة 07 أو 37
  [ "$NEXT" -le $(( NOW + 60 )) ] && NEXT=$(( NEXT + 1800 ))
  if [ $(( NEXT - START )) -gt "$LIMIT" ]; then
    echo "[loop] time limit reached"
    break
  fi
  while [ "$(date +%s)" -lt "$NEXT" ]; do
    if manual_waiting; then
      echo "[loop] manual run waiting: handing over"
      exit 0
    fi
    sleep $(( NEXT - $(date +%s) < 60 ? NEXT - $(date +%s) : 60 ))
  done
done
