#!/usr/bin/env bash
set -u
cd /d/deepseek-harness/.dsh/skills/human-bio-psych-refs || exit 1
export PYTHONIOENCODING=utf-8
PY=/d/deepseek-harness/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe

echo '=== 1 DOI ==='
"$PY" scripts/ref_check.py --doi '10.1016/S0140-6736(12)61031-9' --style all 2>&1 | head -30

echo '=== 2 PMID ==='
"$PY" scripts/ref_check.py --pmid 22818936 2>&1 | head -12

echo '=== 3 TITLE ==='
"$PY" scripts/ref_check.py --title "About sleep's role in memory" --style apa 2>&1 | head -14

echo '=== 4 FAKE DOI ==='
"$PY" scripts/ref_check.py --doi '10.9999/fake.doi.2020.1234' 2>&1 | head -8

echo '=== 5 GB ==='
"$PY" scripts/ref_check.py --title 'Sleep duration and all-cause mortality: a systematic review and meta-analysis of prospective studies' --style gb 2>&1 | head -12
