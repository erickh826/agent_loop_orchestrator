# Loop Engineer 操作指南（給 agent）

你負責替使用者操作本機的 **Loop Engineer orchestrator**。它用來管理多個開發專案的自動化 loop，並且管理一個「GUI 任務佇列」，決定什麼時候可以操作 Windows 桌面。
請嚴格照本指南操作。遇到本指南沒有涵蓋的情況，停下來回報使用者，不要自己發明做法。

---

## 1. 基本資訊

- 主程式：`python D:\tools\loop\loop_orchestrator.py <指令>`（以下簡寫為 `loop <指令>`）
- 遠端觸發用的包裝腳本：`powershell -NoProfile -ExecutionPolicy Bypass -File D:\tools\loop\run_now.ps1 -Project <名稱> -NoPause`
  - **一定要加 `-NoPause`**，否則腳本會停在「Press Enter」等待輸入。
  - 腳本的 exit code 和 python 的 exit code 相同。
- 不要設定或修改環境變數 `LOOP_HOME`、`LOOP_GUI_IDLE_MINUTES`。
- 每個專案的資料都放在 `<專案路徑>/.loop/`：
  - `state.json`：目前狀態
  - `orchestrator.log`：主 log
  - `runs/`：每次 agent 執行的完整輸出

## 2. Exit code 對照表

| code | 意義 | 你該怎麼做 |
| :--- | :--- | :--- |
| 0 | 成功 | 繼續 |
| 1 | 錯誤（phase 失敗、缺 prompt、找不到執行檔、timeout） | 見第 4 節 |
| 2 | 參數錯誤 / 專案未註冊 | 檢查指令和專案名稱（`loop status` 可以列出所有專案），不要重試同一個錯誤指令 |
| 3 | 該專案正在執行中 | **不要**刪除 `run.lock`，也不要結束任何 process。等待後再查 `loop status` |
| 4 | 桌面目前不可用 | 見第 5 節 |
| 5 | 沒有 pending 的 GUI 任務 | 沒事可做，結束 |
| 6 | 桌面已鎖定 | 回報使用者，不要嘗試解鎖 |

## 3. 查詢狀態

```
loop status --json                      # 全部專案
loop status --project <名稱> --json     # 單一專案
```
JSON 欄位：`project`、`path`、`phase`、`refactor_retries`、`max_retries`、`last_run`、`status`、`running`（是否正在執行）、`error`。

Phase 流程（每次 `run` 只往前走一個 phase）：
```
01_planning → 02_implement → 03_qa_review → (04_refactor ↔ 03_qa_review，最多 3 次) → 05_done 或 06_failed_requires_human
```

## 4. 推進專案 loop

1. 先執行 `loop status --project <名稱> --json`。如果 `running` 是 true，就不要啟動。
2. 執行一個 phase：`loop run --project <名稱>`（或用 run_now.ps1 加 `-NoPause`）。
   - 單一 phase 可能要跑 **15–20 分鐘**，請耐心等它結束。**不要**在同一個專案上同時再啟動一次。
   - 輸出最後的區塊會顯示 `RAN PHASE`、`NEXT PHASE`、`RETRIES`、`EXIT CODE`。
3. 根據結果決定下一步：
   - **exit 0，且 NEXT PHASE 不是 05/06**：如果使用者要你一路跑完，就重複第 2 步。
   - **exit 0，且 phase 是 `05_done`**：回報「完成」，停止。
   - **exit 0，且 phase 是 `06_failed_requires_human`**：回報「需要人工處理」，停止。**未經使用者同意，不可以執行 `reset`**。
   - **exit 0，且輸出是 "Nothing to do"**：專案已經在終點了，回報並停止。
   - **exit 1**：讀 `<專案>/.loop/orchestrator.log` 最後約 30 行，判斷原因：
     - log 裡有 `STOP notice` / `STOPPED`：使用者還沒填 `memo/project_context.md` 的 Task Scope。**不要重試**，請使用者填寫。
     - log 裡有 `TIMEOUT`、`not found`（找不到執行檔或 prompt），或是 agent 失敗：最多重試 **1 次**；還是失敗就回報使用者，並附上 log 摘要。
   - **exit 3**：見第 2 節，不要搶鎖。
4. 不確定時，可以先用 `loop --dry-run run --project <名稱>`。它只印出將要執行的指令，不會真的執行，也不會改變狀態。

### 選擇由哪個 CLI 執行（agent）
每個 phase 由哪個 CLI 執行是可以設定的。可用的 agent 定義在 `D:\tools\loop\agents.json`，目前有 agy、claude、codex、kimi、gemini、grok、copilot。
```
loop agents                                   # 列出所有 agent、是否已安裝，以及每個專案每個 phase 用哪一個（--json 可輸出機器格式）
loop run --project <名稱> --agent kimi         # 只有這一次改用 kimi；之後不會保留
loop assign --project <名稱> 03_qa_review=kimi 04_refactor=gemini   # 長期設定這個專案（只有使用者要求時才做）
loop assign --project <名稱> --clear all       # 清除專案設定，回到預設（只有使用者要求時才做）
```
- 使用 run_now.ps1 時，對應參數是 `-Agent <名稱>`。
- 執行 `run` 時，輸出的 `RAN PHASE` 那一行會標示這次實際使用的 agent。
- `loop status --json` 的 `agents` 欄位是各 phase 實際會用的 agent，`agent_overrides` 是這個專案的自訂設定。
- agent 名稱打錯會得到 exit 2。agent 的執行檔找不到則是 exit 1，`loop agents` 會顯示 `NOT FOUND`。
- **使用者沒有指定時，不要自己換 agent。** 如果某個 agent 在同一個 phase 連續失敗，可以向使用者建議改用其他 agent，但要等使用者同意。

### 讓專案重新進入 loop（只有使用者明確要求時才做）
```
loop reset --project <名稱>                         # 回到 03_qa_review，retries 歸 0
loop reset --project <名稱> --phase 01_planning     # 從頭規劃
```
**永遠不要直接編輯 `state.json`**，一律用 `reset`。

## 5. GUI 任務佇列

GUI 任務是需要操作 Windows 桌面的工作。整台電腦共用一個佇列，同一時間只能執行一個任務。

### 5.1 新增任務（任何環境都可以執行）
```
loop gui add --project <名稱> "<清楚、完整的操作說明>"
```
stdout 會印出新任務的 id。

### 5.2 領取並執行任務（只能由在 Windows 桌面上操作的 agent 執行）
`gui next` **必須在使用者登入的互動桌面 session 裡執行**，例如 computer-use agent 在桌面開的終端機。透過 SSH、服務或背景排程執行時，一律會得到 exit 4。

1. 執行 `loop gui next`。
2. **exit 0** 時，輸出格式如下：
   ```
   ID: <任務id>
   PROJECT: <專案名稱>
   PROJECT_PATH: <專案路徑>
   INSTRUCTIONS:
   <操作說明，可能有多行>
   ```
   照 INSTRUCTIONS 執行任務。
3. 任務結束時，**不論成功或失敗，都一定要回報**。否則任務會一直停在 running，擋住整個佇列：
   ```
   loop gui done <任務id> --result "<結果摘要，例如輸出檔路徑>"
   loop gui fail <任務id> --reason "<失敗原因>"
   ```
4. 回報完成後，可以再執行 `loop gui next` 領取下一個任務。exit 5 表示沒有任務了。

### 5.3 `gui next` 被拒絕時
- **exit 4**：輸出會說明原因。
  - 「使用者正在用電腦」（i_am_here.flag）或「閒置時間不足」：不要重試，**至少等 10 分鐘**再試，或直接回報使用者。
  - 「已經有任務在 running」：先執行 `loop gui list` 查看是哪一個。如果那是你自己剛才領的任務，就補上 done 或 fail；如果不是，回報使用者，不要擅自 fail 別人的任務。
  - 「session 不符」：表示你不在桌面 session 裡，改由桌面上的 agent 執行。
- **exit 6**：桌面已鎖定，回報使用者。
- **絕對不要**為了繞過檢查而刪除 `i_am_here.flag`、修改 `LOOP_GUI_IDLE_MINUTES`，或執行 `loop here off`。這些只有使用者本人可以決定。

### 5.4 其他 GUI 指令
```
loop gui list        # 查看所有任務和狀態（pending / running / done / failed）
```
`loop here on` / `loop here off` 是使用者標示「我正在用電腦」的開關。**只有使用者要求時才執行**。

## 6. 禁止事項總整理

- 不要直接修改 `.loop/` 底下的任何檔案（`state.json`、`run.lock`、`agents/`）。
- 不要修改 `D:\tools\loop\gui_queue\` 底下的 JSON 檔，一律透過 `loop gui ...` 指令操作。
- 不要在同一個專案上同時執行兩個 `run`。
- 不要結束其他人的 process 或刪除 lock 檔。
- 沒有使用者同意，不要執行 `reset`、`register`、`assign`、`here on/off`，也不要修改 `agents.json`。
- 回報時要附上實際的 exit code 和關鍵輸出，不要只說「成功」或「失敗」。
