# Install Starployment

Download a native application ZIP from this repository's release. Python, Git,
Node, and a separate Chromium installation are not needed for a native bundle.
The application includes its browser and starts with an empty candidate profile.

## Windows 10 or 11, 64-bit Intel or AMD

1. Download **Starployment-windows-x64.zip** and use **Extract all**. Move the
   extracted **Starployment** folder to a permanent location, such as your user
   Applications folder. Keep the entire folder together, including `_internal`.
2. Open **Starployment.exe**. You can create a desktop shortcut to it.
3. If Windows shows a download reputation warning, verify that the ZIP came from
   this repository's release before using **More info → Run anyway**. The build
   has no commercial code-signing certificate.

Native ARM Windows is not a separate build target. Use the x64 app only where
Windows supports x64 emulation.

## macOS, Apple Silicon or Intel

1. Choose **Starployment-macos-arm64.zip** for an M-series Mac, or
   **Starployment-macos-x64.zip** for an Intel Mac. Check **Apple menu → About
   This Mac** if unsure.
2. Extract the ZIP and drag **Starployment.app** into **Applications**.
3. Open it. The app is ad-hoc signed, with no Apple Developer ID notarization.
   If macOS blocks the first launch, verify the source of the download, then
   open **System Settings → Privacy & Security → Open Anyway** and confirm.
   This is a one-time step for that downloaded build. Do not disable Gatekeeper.

The native release builds target macOS 15 or later. Older Macs can use the
source installation below when their Python and Qt versions are supported.

## First run

The setup wizard opens automatically. Import your resume PDF, confirm the parsed
facts, choose job titles and locations, and fill the answers you know. Unknown
eligibility or required answers remain unset and stop applications for review.
You can return through **Edit Setup** or change everything in Profile/Settings.

**Connect** is optional. In the wizard or **Settings → Connections**, choose an
existing Claude/ChatGPT account or an API key, then use the supplied **Get the
app**, **Sign in**, and **Test** actions. A paid account is your own choice, and
API usage can incur charges from your provider. No account or token ships with
Starployment. You can discover roles and prepare factual applications without
an AI connection.

To track replies or complete verification, enter your own email details in the
same Connections page. Its app-password help opens your mail provider's page;
**Test** checks your settings. Your main email password is not needed.

Automatic applications are off on a new profile. Start by finding jobs, preparing
one, reviewing its answers/resume, and approving it yourself. Only employer
confirmation records an application as Applied. CAPTCHAs and unsupported forms
remain for you to finish on the employer's website.

## Your data and updating

The app stores your profile locally in `%LOCALAPPDATA%\Starployment` on Windows,
or `~/Library/Application Support/Starployment` on macOS. An existing `JobHub`
data folder continues to be used in place. Passwords and API keys remain in
your local private store or, if enabled, the OS credential vault.

Before replacing an installed app, finish active sends and quit Starployment.
Replace only the application folder/bundle; retain your data folder. Open the
new app to continue with the same profile. Do not copy another person's data
folder as a way of sharing the app.

Uninstall by quitting and removing the application. Your data is retained until
you choose to remove its data folder. Removing that folder deletes your local
profile and history, so back it up first if you want to keep them.

## Source installation

Install Git and uv from their official instructions, then run:

```sh
git clone https://github.com/matthewstar05/starployment.git
cd starployment
uv sync --frozen
uv run playwright install chromium
```

On Windows, run `run_desktop.bat`. On macOS, run `./run_desktop.sh`.
For a release source ZIP, extract it and run the last two commands inside the
extracted folder. Source installs use Python 3.12, managed by uv, and keep their
candidate data in the checkout's `data/` folder unless explicitly overridden.
