Great, write an implementation plan markdown file for each of the 4 individual modules.

What does fallback / degradation mean in the document?

docs/ARCHITECTURE.md

So do we need to create two separate folders in this repo for the two FastAPI services, and create separate venvs for each?

What is your recommendation? Or should we keep them in separate repos?

So we only need a single GCE VM?

Stop,

Because my GCP learning resources can be reclaimed at any time.

I do not want to store data on GCP.

Regarding PostgreSQL, I'd like to replace it with OCI Free Tier MySQL. Do you think that's feasible?

No, my free tier MySQL is not deployed on an OCI VM, but is OCI's managed MySQL Database Service product. Please check it for me first using OCI skills/tools.

===========
  1 Container Package / Image Project

  Project Name:

  ghcr.io/nvd11/my-litellm-svc

  This build produces a multi-arch image manifest:

  amd64 image
  arm64 image

  And attaches two tags to the same build result:

  sha-00d8238...
  latest

  Which manifest do these two tags point to respectively?

=================

Great, adjust my config.yaml:

1. Remove the gemini-3.6-flash configuration.
We only use 3.7 flash.

2. Prioritize rotating between free1 and free2 API keys normally.

3. If both free1 and free2 encounter 429 (or other issues), fall back to the pro-plan API key.

4. If the pro-plan key also hits 429, fall back to free3 as the final safety net.

5. Add:
- OPENAI_API_KEY_FREE_1: Primary Account 1 (Owner)
- OPENAI_API_KEY_FREE_2: Primary Account 2 (Spouse)
- OPENAI_API_KEY_PRO_PLAN: Primary Google AI Pro Account
- OPENAI_API_KEY_FREE_3: Ultimate Emergency Fallback Account
Include these key descriptions as opening comment documentation.
