#!/usr/bin/env bash
# 代码变更检测 + 条件测试（2026-10-22 新增）
#
# 背景：quant-lab-iteration 每 6 小时巡检一次，原步骤 5 是「跑一遍全部
# 84 个用例」。这个做法有两个问题：
#   1. 一天跑 4 次，但代码通常一整天没变 —— 纯浪费。
#   2. 用户定下过「不跑全量测试，只跑定向」的规矩，把全量测试塞进定时
#      任务是给每个 tick 都挂一份，跟那条规矩相冲突。
# 改法：先算 core/ llm/ scripts/ tests/ 下所有 .py 的联合哈希，与上次
# 跑测试时的基线比对；只有变了才跑测试。哈希而非 mtime —— 改了又改回
# 去不该算变更，mtime 会误判。
#
# 用法：
#   scripts/check_tests_if_changed.sh          # 有变更才跑
#   scripts/check_tests_if_changed.sh --force  # 强制跑
#
# 输出约定（给 cron 的 LLM 看）：
#   "代码未变更，跳过测试（基线 xxxx）"        -> 正常
#   "代码有变更 xxxx -> yyyy，跑测试…" + 结果   -> 有变更
#   退出码 0 = 无需处理；1 = 测试失败（需要人工看）
set -uo pipefail
cd /opt/quant-lab

PY=/opt/quant-lab/venv/bin/python
BASELINE="data/.code_hash"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

# 联合哈希：文件路径 + 内容，排序后整体再哈希
cur_hash() {
    find core llm scripts tests -name "*.py" -type f 2>/dev/null \
        | sort | xargs md5sum 2>/dev/null | md5sum | cut -d' ' -f1
}

CUR=$(cur_hash)
OLD=$(cat "$BASELINE" 2>/dev/null || echo "")

if [ "$FORCE" -eq 0 ] && [ "$CUR" = "$OLD" ]; then
    echo "代码未变更，跳过测试（基线 ${CUR:0:12}）"
    exit 0
fi

if [ "$FORCE" -eq 1 ]; then
    echo "强制跑测试（当前 ${CUR:0:12}）"
else
    echo "代码有变更 ${OLD:0:12} -> ${CUR:0:12}，跑测试…"
fi

# 分两批跑，避免单次输出过长；失败立即记录但继续跑完，便于一次看全
FAILED=""
run_group() {
    local pat="$1" label="$2"
    local out
    out=$("$PY" -m unittest discover -s tests -p "$pat" 2>&1)
    local rc=$?
    local summary
    summary=$(echo "$out" | grep -E "^Ran [0-9]+ tests|^OK$|^FAILED" | tr '\n' ' ')
    echo "  [${label}] rc=${rc} ${summary}"
    if [ "$rc" -ne 0 ]; then
        FAILED="${FAILED}${label} "
        echo "$out" | tail -25 | sed 's/^/    /'
    fi
}

# 单个文件必须用 "tests.xxx" 模块名加载：discover -p 用完整文件名匹配不上
# （实测 -p "test_book_isolation.py" 会 NO TESTS RAN）
run_file() {
    local f="$1" label="$2"
    local mod="tests.$(basename "$f" .py)"
    local out
    out=$("$PY" -m unittest "$mod" 2>&1)
    local rc=$?
    local summary
    summary=$(echo "$out" | grep -E "^Ran [0-9]+ tests|^OK$|^FAILED" | tr '\n' ' ')
    echo "  [${label}] rc=${rc} ${summary}"
    if [ "$rc" -ne 0 ]; then
        FAILED="${FAILED}${label} "
        echo "$out" | tail -25 | sed 's/^/    /'
    fi
}

# 按被测对象分组，便于定位
run_group "test_*gate*.py"      "风控闸门"
run_group "test_promote.py"     "晋升"
run_group "test_factor*.py"     "因子库"
run_group "test_proposal*.py"   "提案池"

# 注意：这些文件是 pytest 风格（模块级 def test_xxx，不是 unittest.TestCase），
# `python -m unittest` 加载不到、会报 NO TESTS RAN。之前 iteration 巡检只跑
# unittest 的 84 个用例，这类用例一直没进过自动化 —— 是覆盖盲区，这里补上。
# 新增 pytest 风格测试文件时，**必须**同时加进下面两处：PYTEST_FILES 和
# 循环里的 case 排除列表，否则会被 run_file 接手并误报 NO TESTS RAN。
PYTEST_FILES="tests/test_book_isolation.py tests/test_notify_book_label.py
tests/test_rebalance_calendar.py tests/test_resonance_strategy.py tests/test_news_adjust.py"
pytest_run() {
    local out rc summary
    out=$("$PY" -m pytest $PYTEST_FILES -q 2>&1)
    rc=$?
    summary=$(echo "$out" | grep -E "passed|failed" | tail -1)
    echo "  [pytest 组] rc=${rc} ${summary}"
    if [ "$rc" -ne 0 ]; then
        FAILED="${FAILED}pytest "
        echo "$out" | tail -30 | sed 's/^/    /'
    fi
}
pytest_run

# 其余 unittest 文件逐个跑
for f in $(ls tests/test_*.py 2>/dev/null); do
    b=$(basename "$f")
    case "$b" in
        test_*gate*.py|test_promote.py|test_factor*.py|test_proposal*.py) continue ;;
        test_book_isolation.py|test_notify_book_label.py|test_rebalance_calendar.py|test_resonance_strategy.py|test_news_adjust.py) continue ;;
    esac
    run_file "$f" "${b#test_}"
done

# 无论成败都推进基线：测试失败不代表代码没变，下次不该重复跑同一份
echo "$CUR" > "$BASELINE"

if [ -n "$FAILED" ]; then
    echo "测试失败：${FAILED}（基线已推进到 ${CUR:0:12}）"
    exit 1
fi
echo "全部测试通过（基线推进到 ${CUR:0:12}）"
exit 0
