# Pokémon Deal Hunter: setup, step by step

You do everything in a web browser. Nothing gets installed on your computer.

How it works: a robot on GitHub searches eBay, works out which exact card each listing is, looks up the Near Mint market price (TCGplayer, through the free Pokémon TCG API), lowers it for Lightly Played / Played / Damaged, and saves listings priced at or below 70% of that value. A website (GitHub Pages) shows the list.

Total time: about 20 minutes.

---

## Part 1. Get your eBay keys (you already have the account)

1. Go to https://developer.ebay.com and sign in.
2. Click your name (top right), then **Application Keys**.
3. Find the **Production** column (not Sandbox). Click **Create a keyset** if there is not one yet.
4. If eBay shows **Marketplace Account Deletion**, pick the option saying you do **not** store eBay user data (the exemption), and confirm. Production keys stay locked until this is done.
5. Copy these two values into a notes app for the next 10 minutes:
   - **App ID (Client ID)**
   - **Cert ID (Client Secret)**

Copy them without extra spaces at the start or end.

## Part 2. Create the GitHub project

1. Go to https://github.com and sign up (free). Remember your username.
2. Click the **+** at the top right, then **New repository**.
3. Repository name: `pokemon-deal-hunter`. Choose **Public**. Leave everything else alone. Click **Create repository**.
   (It must be Public. GitHub Pages is free only for public repositories. Your keys are not exposed by this, see "Are the keys safe" at the bottom.)
4. On the new empty page, click **uploading an existing file**.
5. On your computer, unzip `pokemon-deal-hunter.zip`. Open the unzipped folder.
6. Show hidden files so you can see the `.github` folder: Mac press **Cmd + Shift + .** (period). Windows File Explorer, View, Show, Hidden items.
7. Select everything inside the folder (`.github`, `data`, `docs`, `README.md`, `scan.py`, `settings.json`, `WORKFLOW_COPY_PASTE.txt`) and drag it all into the GitHub upload box. Wait for the upload to finish.
8. Scroll down and click **Commit changes**.
9. Check the file list shows `.github`, `data`, `docs`, `scan.py`, `settings.json`. If `.github` is missing:
   - Click **Add file, Create new file**.
   - In the name box type exactly `.github/workflows/scan.yml` (typing the slashes creates the folders).
   - Open `WORKFLOW_COPY_PASTE.txt`, copy everything, paste it into the big box.
   - Click **Commit changes**.

## Part 3. Put your eBay keys in GitHub Secrets

1. In your repository click **Settings** (top row of tabs).
2. Left menu: **Secrets and variables**, then **Actions**.
3. Click **New repository secret**.
   - Name: `EBAY_CLIENT_ID`  Secret: your App ID. Click **Add secret**.
4. Click **New repository secret** again.
   - Name: `EBAY_CLIENT_SECRET`  Secret: your Cert ID. Click **Add secret**.

The names must match exactly, capital letters and underscores included.

Optional: get a free key at https://dev.pokemontcg.io and save it as a third secret named `POKEMONTCG_API_KEY`. It makes price lookups faster and steadier.

## Part 4. Let the robot save its results

1. Still in **Settings**, left menu: **Actions**, then **General**.
2. Scroll to **Workflow permissions**. Select **Read and write permissions**. Click **Save**.

## Part 5. Turn on the website

1. In **Settings**, left menu: **Pages**.
2. Under **Build and deployment**, Source: **Deploy from a branch**.
3. Branch: **main**. Folder: **/docs**. Click **Save**.
4. Wait about a minute and refresh the page. A box appears with your address:
   `https://YOUR-USERNAME.github.io/pokemon-deal-hunter/`
5. Bookmark it. This is the link you send to friends.

## Part 6. Run the first scan (this is also your test)

1. Click the **Actions** tab.
2. If GitHub shows a green button saying it needs to enable workflows, click it.
3. Left side: click **Scan eBay**.
4. Click **Run workflow** (gray button on the right). A box opens with one field. For this first test type **95** in it, then click the green **Run workflow**.
   (95 means "show me nearly everything it found, even normal prices". That proves the whole pipeline works. It only applies to this one run.)
5. Wait about 2 to 3 minutes, refresh the page. A new row appears. Wait for the icon to turn into a green check.
6. Click that row, click **scan**, click **Run scanner**. You should see lines like:
   - `Search: 1800 unique listings from 20 searches`
   - `First pass: 240 listings matched to a card ...`
   - `Saved 120 deals. eBay calls this run: ...`
7. Open your website and refresh. You should see listings. Move the **Priced at or below** slider up to 95 to see them all.

If you see deals, it works. From now on it runs by itself and uses your normal 70% setting. The first hour or two shows few deals because it is still learning card prices (about 60 new cards per run).

### If the run shows a red X

Click the run, then **scan**, then **Run scanner**, and read the last lines:

| What you see | What to do |
|---|---|
| `Missing EBAY_CLIENT_ID` | A secret name is wrong or missing. Redo Part 3, check spelling. |
| `Could not get an eBay token (HTTP 401)` | Keys are wrong. Make sure you copied the **Production** keys, no spaces. Redo the two secrets. |
| `Could not get an eBay token (HTTP 400)` or `invalid_scope` | Your Production keyset is not active yet. Finish the Marketplace Account Deletion step in Part 1. |
| `eBay search failed ... HTTP 403` | Same cause. Production access is not unlocked yet. |
| `eBay search failed ... HTTP 429` | eBay rate limit. Wait an hour, or raise "Run every" on the site. |
| `Saved 0 deals` | Normal with the strict 70% setting on a quiet day. Repeat with 95 in the box to prove it works. |
| `remote: Permission denied ... 403` on "Save results" | Redo Part 4. |

Copy the red text and send it to Claude if you get stuck.

## Part 7. Use the website

- **Priced at or below**: price as a percent of the card's value in its stated condition. At 70%, a card worth $100 shows up at $70 or less.
- **Saves at least**: dollars under value, shipping included.
- **Confidence**: how sure we are of the condition, nothing else. Near Mint stated in several places is high. Lightly Played is capped at 80. MP, HP and Damaged are always low. Tap **Why this score** to see the evidence.
- **Warning tags** (new seller, low rating, ships from overseas, far below market) never change confidence. Read them before buying.
- **Open on eBay** and **Check market price** let you verify every deal yourself.

## Part 8. (Optional) Scan now button, run speed, and settings from the site

Needed only for the **Scan now** button and the **Scanner settings** box. The sliders work without it.

1. On GitHub click your profile picture (top right), **Settings**.
2. Left menu, bottom: **Developer settings**, **Personal access tokens**, **Fine-grained tokens**, **Generate new token**.
3. Name: `deal-hunter`. Expiration: 1 year.
4. Repository access: **Only select repositories**, choose `pokemon-deal-hunter`.
5. Repository permissions: set **Contents** to **Read and write** and **Actions** to **Read and write**.
6. Click **Generate token** and copy it (you only see it once).
7. On your website open **Scanner settings**. Enter `YOUR-USERNAME/pokemon-deal-hunter`, paste the token, click **Load settings**.
8. Now you can change **Run every** (5, 10, 15, 30, 60 minutes), the flag percent, the searches, and the price range, then **Save settings**. **Scan now** at the top starts a scan right away, and **Scan now with this** runs one scan at a different percent without changing your saved one.

The token is stored only in your browser. Do not do this on a shared computer.

---

## Are the keys safe on GitHub?

Yes, as long as they are in **Secrets** (Part 3) and not in any file.
- Secrets are encrypted. After you save them nobody can read them back, not even you.
- GitHub hides them in the run logs.
- A public repository does not expose secrets. People can see your code and the deals list, not your keys.
- Never paste a key into a file, the README, an issue, or the website.
- If you ever think a key leaked, go to developer.ebay.com, regenerate the keyset, and update the two secrets.
- The eBay keys here can only read public listings. They cannot buy anything or touch your eBay account.

## Limits to know about

- Prices are TCGplayer's market price, mostly Near Mint, updated about daily. Thin-market cards can be off. Always check.
- Cards are matched by the number in the title (like 58/102). Listings without a number are skipped.
- Condition is read from eBay's condition field, the title, and the description. Photos are not checked.
- eBay's free tier allows about 5,000 calls a day. At fast run rates the scanner rotates through your searches to stay under it. The settings box shows the effect.
- GitHub can pause scheduled robots on repositories with no activity for 60 days. If it stops, open the Actions tab and re-enable it.
- Without the token, the website can take up to 5 minutes to show a fresh scan.
