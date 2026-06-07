param(
    [switch]$RunOnce,
    [int]$IntervalMinutes = 180
)

Write-Host "Starting JobPulse LinkedIn Scheduler..." -ForegroundColor Cyan

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

Write-Host "Project root: $ProjectRoot"

$VenvActivate = Join-Path $ProjectRoot "venv\Scripts\Activate.ps1"

if (Test-Path $VenvActivate) {
    Write-Host "Activating virtual environment..."
    . $VenvActivate
} else {
    Write-Host "Virtual environment not found at: $VenvActivate" -ForegroundColor Yellow
    Write-Host "Continuing with current Python environment..."
}

Write-Host "Starting PostgreSQL and Adminer..."
docker compose up -d db adminer

$env:POSTGRES_HOST = "localhost"
$env:POSTGRES_PORT = "5432"
$env:POSTGRES_DB = "jobpulse"
$env:POSTGRES_USER = "jobpulse_user"
$env:POSTGRES_PASSWORD = "jobpulse_password"

$env:LINKEDIN_BROWSER = "chrome"
$env:LINKEDIN_STALE_DAYS = "7"
$env:LINKEDIN_SCHEDULE_INTERVAL_MINUTES = "$IntervalMinutes"

Write-Host "Database host: $env:POSTGRES_HOST"
Write-Host "Schedule interval: $env:LINKEDIN_SCHEDULE_INTERVAL_MINUTES minute(s)"

if ($RunOnce) {
    Write-Host "Running scheduler once..." -ForegroundColor Yellow
    python -m scripts.linkedin_scheduler --run-once
} else {
    Write-Host "Running scheduler continuously..." -ForegroundColor Green
    Write-Host "Press Ctrl + C to stop."
    python -m scripts.linkedin_scheduler --interval-minutes $IntervalMinutes
}