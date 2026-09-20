# Loaded only by the recorder's interactive Bash. No permanent user changes.
if [[ -f "$HOME/.bashrc" ]]; then
    source "$HOME/.bashrc"
fi

# Remove common aliases before defining the session's protected commands.
unalias clear reset 2>/dev/null || :
clear() {
    printf '%s\n' '[Linux Recorder] clear blocked: 녹화 중에는 화면 기록을 지울 수 없습니다.' >&2
    return 1
}
reset() {
    printf '%s\n' '[Linux Recorder] reset blocked: 녹화 중에는 화면 기록을 지울 수 없습니다.' >&2
    return 1
}
readonly -f clear reset
export -f clear reset

# Ignore Ctrl+L without deleting the command currently being typed. Cover both
# Emacs and Vi editing modes without changing the user's selected editing mode.
bind -m emacs-standard '"\C-l": ""'
bind -m vi-insert '"\C-l": ""'
bind -m vi-command '"\C-l": ""'
printf '%s\n' \
    '[실습 기록 안내] 과제 제출을 위해 직접 제작한 Linux Recorder를 사용합니다.' \
    '실제 터미널 화면을 캡처해 명령어와 결과가 이어지는 PNG/PDF로 저장합니다.' \
    '기록 유실 방지를 위해 이 기록용 Bash에서 clear/reset/Ctrl+L을 제한합니다.' \
    'clear/reset 차단 안내는 Linux 자체의 오류가 아니라 기록 프로그램의 보호 기능입니다.'
