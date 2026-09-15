$projectPath = Split-Path -Parent $MyInvocation.MyCommand.Path

# 清除舊 container（若存在）
docker rm -f n8n 2>$null

# 啟動 n8n (Docker)
Start-Process powershell -ArgumentList "-NoExit", "-Command", "docker run --rm --name n8n -p 5678:5678 -v ${env:USERPROFILE}\.n8n:/home/node/.n8n docker.n8n.io/n8nio/n8n" -WindowStyle Normal

# 等 n8n 起來
Start-Sleep -Seconds 5

# 啟動 Flask（用 WorkingDirectory 避開中文路徑問題）
Start-Process powershell -ArgumentList "-NoExit", "-Command", "python app.py" -WorkingDirectory $projectPath -WindowStyle Normal

Write-Host "n8n (port 5678) 和 Flask (port 5001) 已啟動" -ForegroundColor Green
Write-Host "網頁：http://127.0.0.1:5001" -ForegroundColor Cyan
Write-Host "n8n：http://localhost:5678" -ForegroundColor Cyan
