# Personal OS terminal hook for bash (Git Bash) (hook v1).
# Records, per command: the command line, working directory, start/end time and exit status.
# Nothing else: no output, no keystrokes. Lines starting with a space are never recorded, and
# nothing is written while Personal OS is paused or PERSONAL_OS_PAUSE is set.
# Installed by `personal-os install-hooks`; removed by `personal-os uninstall-hooks`.

if [[ -z "${__POS_LOADED:-}" && $- == *i* ]]; then
__POS_LOADED=1
__POS_SPOOL='__SPOOL__'
__POS_MARKER='__MARKER__'
__POS_SESSION="${RANDOM}${RANDOM}$$"
__POS_LAST_HIST=""
__POS_START=""
__POS_ARMED=""
# The private-command convention relies on bash's ignorespace: a line starting with a space
# never enters history, so the hook never sees it. Add it if the user's HISTCONTROL lacks it.
case ":${HISTCONTROL:-}:" in
  *:ignorespace:*|*:ignoreboth:*) ;;
  *) HISTCONTROL="${HISTCONTROL:+$HISTCONTROL:}ignorespace" ;;
esac

# JSON-escape $1 into __POS_J (no subshell: forks are slow on Windows).
__pos_json() {
  local s=$1
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\r'/\\r}; s=${s//$'\t'/\\t}
  __POS_J=$s
}

__pos_debug() {
  [[ -n "$__POS_ARMED" && -z "${COMP_LINE:-}" ]] || return 0
  __POS_ARMED=""
  __POS_START=$EPOCHREALTIME
}

__pos_precmd() {
  local ec=$? line num cmd start end jcmd jcwd ok=false
  __POS_ARMED=""
  start=$__POS_START
  __POS_START=""
  [[ -n "$start" ]] || return $ec
  line=$(HISTTIMEFORMAT= builtin history 1)
  [[ $line =~ ^[[:space:]]*([0-9]+)[*]?[[:space:]]+(.*)$ ]] || return $ec
  num=${BASH_REMATCH[1]}
  cmd=${BASH_REMATCH[2]}
  [[ "$num" != "$__POS_LAST_HIST" ]] || return $ec
  __POS_LAST_HIST=$num
  [[ "$cmd" != " "* ]] || return $ec
  [[ ! -e "$__POS_MARKER" && -z "${PERSONAL_OS_PAUSE:-}" ]] || return $ec
  end=$EPOCHREALTIME
  __pos_json "$cmd"; jcmd=$__POS_J
  __pos_json "$PWD"; jcwd=$__POS_J
  [[ $ec -eq 0 ]] && ok=true
  [[ -d "$__POS_SPOOL" ]] || mkdir -p "$__POS_SPOOL" 2>/dev/null
  printf '{"v":1,"shell":"bash","session":"%s","pid":%d,"history_id":%d,"command":"%s","cwd":"%s","start_epoch":"%s","end_epoch":"%s","success":%s,"exit_code":%d,"host":"%s"}\n' \
    "$__POS_SESSION" "$$" "$num" "$jcmd" "$jcwd" "$start" "$end" "$ok" "$ec" \
    "${TERM_PROGRAM:-${WT_SESSION:+WindowsTerminal}}" >> "$__POS_SPOOL/bash-$$.jsonl" 2>/dev/null
  return $ec
}

__pos_ready() { __POS_ARMED=1; }

trap '__pos_debug' DEBUG
PROMPT_COMMAND="__pos_precmd${PROMPT_COMMAND:+; $PROMPT_COMMAND}; __pos_ready"
fi
