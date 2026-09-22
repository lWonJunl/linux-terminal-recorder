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
    '===================== Linux Recorder v1.3.3 =====================' \
    'GitHub: https://github.com/lWonJunl/linux-terminal-recorder' \
    '© 2026 Choi WonJun. Developed with assistance from OpenAI Codex.' \
    '=================================================================' \
    '[기록 안내] Linux 명령어 실행 과정과 결과를 PNG/PDF로 저장합니다.' \
    '[보호 안내] 기록 유실 방지를 위해 clear/reset/Ctrl+L을 제한합니다.'
