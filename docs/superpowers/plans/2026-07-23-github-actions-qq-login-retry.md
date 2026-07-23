# GitHub Actions QQ Login Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the scheduled QQ Bot workflow resilient to transient token-endpoint connection timeouts while keeping live Actions progress visible.

**Architecture:** Keep the mitigation inside `.github/workflows/scheduled-bot.yml`: restore bounded IPv4/IPv6 diagnostics, start a fresh Bot process up to three times with 30/60-second backoff, and run a separate one-minute heartbeat process during each attempt. Deployment configuration tests assert the safety and lifecycle invariants without modifying `qq-botpy` or application code.

**Tech Stack:** GitHub Actions YAML, Bash, Python 3.11, `unittest`, PyYAML

---

## Execution Context

- Worktree: `E:\Agent\.worktrees\travel_agent\image-reservation-reminders`
- Branch: `feature/image-reservation-reminders`
- Design: `docs/superpowers/specs/2026-07-23-github-actions-qq-login-retry-design.md`
- Editing rule: use `apply_patch`; do not modify `main` or the primary checkout.
- Verification baseline: `python -m unittest discover -s tests -v`

## File Responsibility Map

- `.github/workflows/scheduled-bot.yml`: bounded diagnostics, Bot start retries, heartbeat lifecycle, and final exit-status handling.
- `tests/test_deployment_config.py`: static regression checks for the Workflow behavior and secret-safety boundary.

### Task 1: Specify Diagnostic and Retry Invariants

**Files:**
- Modify: `tests/test_deployment_config.py`

- [ ] **Step 1: Add the failing diagnostics test**

Add this method to `DeploymentConfigTests` after `test_scheduled_workflow_installs_full_test_dependencies`:

```python
def test_scheduled_workflow_diagnoses_qq_without_secrets(self):
    workflow = self.workflow_path.read_text(encoding="utf-8")
    self.assertNotIn("# - name: Diagnose QQ connectivity", workflow)
    diagnostic = workflow.split(
        "- name: Diagnose QQ connectivity",
        maxsplit=1,
    )[1].split("- name: Run QQ Bot", maxsplit=1)[0]

    self.assertIn("getent ahosts bots.qq.com", diagnostic)
    self.assertIn("curl -4 -I", diagnostic)
    self.assertIn("curl -6 -I", diagnostic)
    self.assertIn("--connect-timeout 5", diagnostic)
    self.assertIn("--max-time 10", diagnostic)
    self.assertNotIn("QQ_BOT_APPID", diagnostic)
    self.assertNotIn("QQ_BOT_SECRET", diagnostic)
```

The current Workflow keeps the diagnostic step commented out and uses longer timeout values, so the active-step and bounded-timeout assertions must fail.

- [ ] **Step 2: Add the failing retry and heartbeat test**

Add this method immediately after the diagnostics test:

```python
def test_scheduled_workflow_retries_login_and_reports_heartbeat(self):
    workflow = self.workflow_path.read_text(encoding="utf-8")

    self.assertIn("max_attempts=3", workflow)
    self.assertIn(
        'for attempt in $(seq 1 "$max_attempts"); do',
        workflow,
    )
    self.assertIn("delay=$((attempt * 30))", workflow)
    self.assertIn("QQ Bot heartbeat", workflow)
    self.assertIn("sleep 60", workflow)
    self.assertIn('kill "$heartbeat_pid"', workflow)
    self.assertIn(
        'if [ "$status" -eq 124 ] || [ "$status" -eq 130 ]; then',
        workflow,
    )
    self.assertIn(
        "if: always() && steps.run_bot.outcome != 'skipped'",
        workflow,
    )
```

- [ ] **Step 3: Run the focused tests and confirm RED**

Run:

```powershell
python -m unittest tests.test_deployment_config.DeploymentConfigTests.test_scheduled_workflow_diagnoses_qq_without_secrets tests.test_deployment_config.DeploymentConfigTests.test_scheduled_workflow_retries_login_and_reports_heartbeat -v
```

Expected: both tests fail because the diagnostic step is commented and the Workflow has no retry loop or heartbeat.

### Task 2: Restore Diagnostics and Add Process Retries

**Files:**
- Modify: `.github/workflows/scheduled-bot.yml`
- Test: `tests/test_deployment_config.py`

- [ ] **Step 1: Restore bounded, non-failing diagnostics**

Replace the commented diagnostic block with:

```yaml
      - name: Diagnose QQ connectivity
        shell: bash
        run: |
          echo "Installed networking packages"
          python -m pip show qq-botpy aiohttp aiohappyeyeballs

          echo "DNS addresses for bots.qq.com"
          getent ahosts bots.qq.com || true

          echo "QQ token endpoint over IPv4"
          curl -4 -I \
            --connect-timeout 5 \
            --max-time 10 \
            https://bots.qq.com/app/getAppAccessToken || true

          echo "QQ token endpoint over IPv6"
          curl -6 -I \
            --connect-timeout 5 \
            --max-time 10 \
            https://bots.qq.com/app/getAppAccessToken || true
```

The requests do not include Bot credentials. `|| true` ensures DNS, IPv4, or IPv6 diagnostic failure cannot stop the job.

- [ ] **Step 2: Replace the single Bot process with retry and heartbeat control**

In `Run QQ Bot`, keep the existing `RUN_MINUTES` validation and `echo`, then replace everything from `status=0` through the final status check with:

```bash
          max_attempts=3
          status=1

          for attempt in $(seq 1 "$max_attempts"); do
            echo "Starting QQ Bot attempt $attempt/$max_attempts"

            (
              while true; do
                sleep 60
                echo "QQ Bot heartbeat: attempt $attempt/$max_attempts is still running"
              done
            ) &
            heartbeat_pid=$!

            status=0
            timeout --signal=INT --kill-after=30s \
              "${RUN_MINUTES}m" python -u bot.py || status=$?

            kill "$heartbeat_pid" 2>/dev/null || true
            wait "$heartbeat_pid" 2>/dev/null || true

            if [ "$status" -eq 124 ] || [ "$status" -eq 130 ]; then
              echo "Bot reached scheduled stop time"
              exit 0
            fi

            if [ "$status" -eq 0 ]; then
              echo "Bot exited normally"
              exit 0
            fi

            if [ "$attempt" -lt "$max_attempts" ]; then
              delay=$((attempt * 30))
              echo "Bot exited with status $status; retrying in $delay seconds"
              sleep "$delay"
            fi
          done

          echo "Bot exited unexpectedly after $max_attempts attempts with status $status"
          exit "$status"
```

This preserves full configured online time for the first successful attempt. The maximum additional backoff is 90 seconds, which remains within the existing 355-minute job timeout when `RUN_MINUTES` is 340.

- [ ] **Step 3: Run deployment configuration tests and confirm GREEN**

Run:

```powershell
python -m unittest tests.test_deployment_config -v
```

Expected: all deployment configuration tests pass.

- [ ] **Step 4: Validate YAML parsing explicitly**

Run:

```powershell
python -c "from pathlib import Path; import yaml; yaml.safe_load(Path('.github/workflows/scheduled-bot.yml').read_text(encoding='utf-8')); print('workflow yaml parsed')"
```

Expected: `workflow yaml parsed` and exit code 0.

- [ ] **Step 5: Commit the focused Workflow fix**

```powershell
git add .github/workflows/scheduled-bot.yml tests/test_deployment_config.py
git -c user.name="Liwei Zhang" -c user.email="119822948+ademjest@users.noreply.github.com" commit -m "ci: retry QQ bot startup with heartbeat"
```

### Task 3: Run Full Regression and Record Handoff State

**Files:**
- Verify: `.github/workflows/scheduled-bot.yml`
- Verify: `tests/test_deployment_config.py`

- [ ] **Step 1: Run the complete test suite**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 2: Run repository consistency checks**

Run:

```powershell
git diff --check
git status --short --branch
git log --oneline -5
```

Expected:

- `git diff --check` prints nothing.
- the worktree is clean.
- the latest implementation commit is `ci: retry QQ bot startup with heartbeat`.

- [ ] **Step 3: Push only after explicit user authorization**

Do not push automatically. Report the local commit and verification result. If the user authorizes a push, run:

```powershell
git push origin feature/image-reservation-reminders
```

The existing Pull Request, if present, will update automatically.
