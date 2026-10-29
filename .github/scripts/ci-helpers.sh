# Actions 단계에서 `source .github/scripts/ci-helpers.sh` 로 불러 쓰는 공용 함수.

# retry <횟수> <처음 대기(초)> <명령...>
# 일시적인 네트워크·서버 오류를 넘기려고 같은 명령을 다시 시도한다. 대기는 시도마다 두 배.
retry() {
  local tries=$1 delay=$2 n=1 rc
  shift 2
  while true; do
    "$@" && return 0
    rc=$?
    [ "$n" -ge "$tries" ] && { echo "::error::${tries}번 시도했지만 계속 실패했어요 (종료 코드 ${rc})"; return "$rc"; }
    echo "::warning::${n}/${tries}번째 시도 실패 (종료 코드 ${rc}) — ${delay}초 뒤 다시 해요"
    sleep "$delay"
    delay=$((delay * 2)); n=$((n + 1))
  done
}

# push_with_rebase <브랜치> [횟수]
# 올리는 사이 다른 커밋이 먼저 들어가 거절되면 최신 기록 위에 얹어(rebase) 다시 올린다.
# 봇이 만드는 파일은 통째로 새로 생성한 것이라, 같은 곳이 겹치면 봇 쪽을 따른다.
# (-X theirs: rebase에서 theirs 는 '지금 얹는 내 커밋'이다.)
push_with_rebase() {
  local branch=$1 tries=${2:-4} n=1
  while true; do
    git push origin "HEAD:${branch}" && return 0
    [ "$n" -ge "$tries" ] && { echo "::error::push 가 ${tries}번 모두 실패했어요"; return 1; }
    echo "::warning::push 실패 (${n}/${tries}) — 최신 기록을 받아 위에 얹고 다시 올려요"
    if ! git pull --rebase -X theirs origin "$branch"; then
      # 네트워크 때문에 못 받은 것이면 다음 시도로 넘어가고, 얹다가 멈춘 것이면 되돌리고 알린다
      if [ -d "$(git rev-parse --git-path rebase-merge)" ] || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
        git rebase --abort
        echo "::error::최신 기록 위에 얹다가 충돌했어요. 직접 확인이 필요해요"
        return 1
      fi
    fi
    sleep $((n * 5)); n=$((n + 1))
  done
}
