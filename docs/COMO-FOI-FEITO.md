# How Hear AI was made

This project was built by a **team of AI agents coordinated by one person**: a lead agent that plans, delegates and integrates; executor agents that implement in separate working folders; and an independent reviewer that reads the code without changing it. Each task had its goal, allowed files and acceptance criteria written beforehand, and every decision was recorded.

Three lessons, with real examples:

**1. Tests first, and "don't declare it done without evidence."**
For each task the lead ran the full test suite itself and kept the result with a timestamp. When proof was missing, the task did not go in: for example, the "disappearing bubble" was only declared *recovered* in the cases the tests could reproduce (hidden window, zero opacity, off-screen, lost "always on top"); the cause of the real-world, day-to-day case was declared **not proven**, with a diagnostic log to collect the evidence.

**2. Independent review found what the tests missed — and was also wrong at times.**
A reviewer that only reads the code pointed out that the consent to send speech to a cloud server was a plain yes/no: anyone editing the address in the configuration file would reuse the acceptance for another server. The tests passed, but the rule was incomplete; today the acceptance is tied to the address. Another finding rated "high" was false (the interface was already being called on the right thread) and was discarded after checking the code. Review needs triage, not blind obedience.

**3. Agents fail in predictable ways, so whoever integrates must check.**
We saw support files created outside the agreed scope, tests that locked in exactly the wrong behavior (for example, always recording "review = false" in the history), and a defect no test covered: cancelling a recording and starting another right away made the new dictation come out **empty**. It only appeared when someone went looking for edge cases. The rule that stuck: check the diff, run the full suite (today, more than 1,100 tests) and record what remains pending.

The product still has parts **not tested in real use** (varied microphones, screens with other scales, a local language model, downloading the larger model). The `README.md` lists what was and was not verified.
