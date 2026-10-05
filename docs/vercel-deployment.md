# Vercel deployment

## Create the application

1. Fork `https://github.com/antoniaci/blackbird` into the `jaleski01` GitHub account as `blackbird`.
2. Import `jaleski01/blackbird` into Vercel and leave the framework preset as **Other**. Keep `main` as the Production Branch and keep Git deployments enabled.
3. In the Vercel project, enable **Vercel Authentication** for **All Deployments**, including Production. This protects the page and API with your Vercel account.
4. Add the following Production environment variable, then redeploy:

   ```text
   API_URL=https://ai.blackbird.run
   ```

   `INSTAGRAM_SESSION_ID` is optional and is not needed for the web app. Do not upload `.env`; it is deliberately excluded from Git. `.env.example` contains only the public AI service URL and an empty optional Instagram setting.

5. In the fork's GitHub settings, enable GitHub Actions and set **Workflow permissions** to **Read and write permissions** so the scheduled sync can push validated merges to `main`. No GitHub secret is required; the workflow uses its repository-scoped `GITHUB_TOKEN`.
6. Confirm that a push to `main` creates a Production deployment in Vercel. Each successful upstream sync pushes to that branch, so Vercel's Git integration builds the new commit automatically.

## Upstream synchronization

The `Upstream sync` GitHub Actions workflow checks `antoniaci/blackbird`'s `main` branch every 15 minutes and can also be started manually from the Actions tab. It merges changes into the fork, installs the declared dependencies, compiles the Python sources, and runs the offline web/export checks before pushing. A merge conflict, failed check, or concurrent update stops the push; `main` remains on its last accepted commit and Vercel receives no deployment for the rejected revision. GitHub Actions reports the failed run for review.

## Local testing

Use `npx vercel dev --local` to run both the static interface and the Python API locally. A static file server can display the page but cannot serve its API routes, so searches from a static-only preview will fail. Install the pinned dependencies from `requirements.txt` into a Python 3.12 virtual environment before starting the Vercel CLI. Username and email searches do not require an API key or `API_URL`; `API_URL` is only needed if you explicitly enable AI.

## Runtime behavior

- Python 3.12 is selected in `.python-version`; Vercel's Python Functions runtime is currently in Beta.
- Search requests stream newline-delimited events. Site checks stop at 270 seconds so the function can return a clearly marked partial result within Vercel's 300-second Hobby limit.
- Generated exports and the refreshed WhatsMyName list live only in `/tmp` for the duration of a function instance. Downloads are streamed back to the browser.
- The WhatsMyName fallback snapshot is bundled with attribution under CC BY-SA 4.0. Each search tries the upstream data source and falls back to that snapshot when unavailable; refreshes are written only to temporary storage.
- Vercel Authentication and the GitHub repository connection are dashboard settings. The repository cannot create or verify those account-level settings.
