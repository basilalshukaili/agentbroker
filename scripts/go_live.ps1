# =============================================================
# Agent Broker — one-shot launch script (PowerShell, Windows)
# =============================================================
#
# What this does:
#   1. Verifies all credentials (calls validate_credentials.py)
#   2. Initializes git, commits, pushes to your GitHub repo
#   3. Sets all Fly.io secrets
#   4. Deploys to Fly.io
#   5. Maps your domain agent-broker-edge.basil-agent.workers.dev
#   6. Submits to MCP registries via API (Smithery, Glama)
#   7. Prints next steps
#
# Run from project root:
#   pwsh ./scripts/go_live.ps1
#
# Prerequisites you (the human) handle ONCE before running:
#   - Install flyctl: https://fly.io/docs/hands-on/install-flyctl/
#   - Authenticate: `flyctl auth login` OR set FLY_API_TOKEN in .env (DONE)
#   - GitHub repo created at github.com/basilalshukaili/agentbroker (DONE)
# =============================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# Load .env into the PowerShell session
if (Test-Path .env) {
    Get-Content .env | ForEach-Object {
        if ($_ -match "^\s*#") { return }
        if ($_ -match "^\s*$") { return }
        if ($_ -match "^([A-Z_][A-Z0-9_]*)=(.*)$") {
            $key = $Matches[1]
            $value = $Matches[2].Trim('"').Trim("'")
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
}

Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "  AGENT BROKER — GO LIVE" -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host ""

# ---------- Step 1: validate credentials ----------
Write-Host "[1/6] Validating credentials..." -ForegroundColor Yellow
python scripts/validate_credentials.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "Credential validation failed. Fix above issues, then re-run." -ForegroundColor Red
    exit 1
}
Write-Host ""

# ---------- Step 2: git init + push ----------
Write-Host "[2/6] Pushing to GitHub..." -ForegroundColor Yellow
if (-not (Test-Path .git)) {
    git init
    git branch -M main
}

# Verify .env is gitignored
$envIgnored = git check-ignore .env 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  .env is not gitignored. Add it to .gitignore before continuing." -ForegroundColor Red
    exit 1
}
Write-Host "  .env is gitignored."

git add -A
$status = git status --porcelain
if ($status) {
    git commit -m "Deploy: agent-broker live launch"
}

# Make sure remote is set
$remotes = git remote
if ($remotes -notcontains "origin") {
    git remote add origin "$env:GITHUB_REPO.git"
}

git push -u origin main 2>&1 | Tee-Object -Variable pushOutput
if ($LASTEXITCODE -ne 0) {
    Write-Host "Push failed. If this is a credential issue, run 'gh auth login' or set up a PAT." -ForegroundColor Yellow
    Write-Host "Continuing anyway (deployment can proceed locally)." -ForegroundColor Yellow
}
Write-Host ""

# ---------- Step 3: set Fly.io secrets ----------
Write-Host "[3/6] Setting Fly.io secrets..." -ForegroundColor Yellow

$flyToken = $env:FLY_API_TOKEN
if (-not $flyToken) {
    Write-Host "  FLY_API_TOKEN not in env. Skipping secret-set; run 'flyctl auth login' and retry." -ForegroundColor Red
    exit 1
}

# Generate fresh signing secrets if still using dev defaults
$identitySecret = $env:AGENT_IDENTITY_SIGNING_SECRET
if ($identitySecret -like "dev-*") {
    $identitySecret = -join ((48..57) + (97..122) | Get-Random -Count 64 | ForEach-Object { [char]$_ })
}
$billingSecret = $env:BILLING_RECEIPT_SIGNING_SECRET
if ($billingSecret -like "dev-*") {
    $billingSecret = -join ((48..57) + (97..122) | Get-Random -Count 64 | ForEach-Object { [char]$_ })
}

$secretsArgs = @(
    "TWILIO_ACCOUNT_SID=$($env:TWILIO_ACCOUNT_SID)",
    "TWILIO_AUTH_TOKEN=$($env:TWILIO_AUTH_TOKEN)",
    "CALCOM_API_KEY=$($env:CALCOM_API_KEY)",
    "CALCOM_USERNAME=$($env:CALCOM_USERNAME)",
    "VAPI_API_KEY=$($env:VAPI_API_KEY)",
    "VAPI_PUBLIC_KEY=$($env:VAPI_PUBLIC_KEY)",
    "RESEND_API_KEY=$($env:RESEND_API_KEY)",
    "PADDLE_API_KEY=$($env:PADDLE_API_KEY)",
    "POLAR_API_KEY=$($env:POLAR_API_KEY)",
    "BILLING_PROVIDER=paddle",
    "AGENT_IDENTITY_SIGNING_SECRET=$identitySecret",
    "BILLING_RECEIPT_SIGNING_SECRET=$billingSecret",
    "PUBLIC_BASE_URL=https://$($env:DOMAIN)",
    "COMPLIANCE_DEFAULT_JURISDICTION=international",
    "SUPPLY_SEED_MODE=empty",
    "REQUIRE_AUTH=false"
)

flyctl secrets set --config deploy/fly.toml @secretsArgs --stage
Write-Host ""

# ---------- Step 4: deploy ----------
Write-Host "[4/6] Deploying to Fly.io..." -ForegroundColor Yellow
flyctl deploy --config deploy/fly.toml --remote-only
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Deploy failed. Check 'flyctl logs --config deploy/fly.toml' for details." -ForegroundColor Red
    exit 1
}
Write-Host ""

# ---------- Step 5: map domain ----------
Write-Host "[5/6] Mapping domain $env:DOMAIN..." -ForegroundColor Yellow
flyctl certs create $env:DOMAIN --config deploy/fly.toml 2>&1 | Out-Host
Write-Host ""
Write-Host "  Add the DNS records flyctl just printed at https://dash.domain.digitalplat.org" -ForegroundColor Cyan
Write-Host "  Propagation: 5-30 minutes." -ForegroundColor Cyan
Write-Host ""

# ---------- Step 6: submit to registries ----------
Write-Host "[6/6] Submitting to MCP registries..." -ForegroundColor Yellow
python scripts/submit_to_registries.py
Write-Host ""

# ---------- Done ----------
Write-Host "===================================================" -ForegroundColor Green
Write-Host "  LAUNCH COMPLETE" -ForegroundColor Green
Write-Host "===================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Live URL:        https://$env:DOMAIN"
Write-Host "  MCP endpoint:    https://$env:DOMAIN/mcp"
Write-Host "  Health:          https://$env:DOMAIN/health"
Write-Host "  Manifest:        https://$env:DOMAIN/manifest"
Write-Host "  llms.txt:        https://$env:DOMAIN/llms.txt"
Write-Host ""
Write-Host "  Next steps in EXPIRY_CHECKLIST.md."
