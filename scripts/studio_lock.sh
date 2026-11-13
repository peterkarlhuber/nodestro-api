#!/usr/bin/env bash
# Writes the studio lock (nodestro-studio platform/internal-server.txt, the exact commits of the internal packages)
# to the file given as the only argument. CI calls it before every install with the lock.
#
# A pull request reads the lock of the studio branch with its own head branch's name when there is one (a change that
# needs newer components and the studio bump that moves them land as a pair), else the studio master's. Pushes, the
# nightly and dispatched runs have no head branch and read the studio master's lock only. A paired lock is made
# visible: a warning names the studio branch, and the job summary shows its diff against the studio master's lock.
set -euo pipefail

out=${1:?usage: studio_lock.sh <output file>}
base=https://raw.githubusercontent.com/peterkarlhuber/nodestro-studio
path=platform/internal-server.txt
branch=${GITHUB_HEAD_REF:-}

if [ -n "$branch" ] && curl -fsSL "$base/$branch/$path" -o "$out.paired"; then
  curl -fsSL "$base/master/$path" -o "$out.master"
  echo "::warning title=Paired studio lock::This run installs with the lock of the studio branch '$branch', not the studio master's; see the job summary for the diff."
  {
    echo "### Paired studio lock"
    echo
    echo "This run installs with the lock of the studio branch \`$branch\`, not the studio master's."
    echo
    if diff -u --label "master/$path" --label "$branch/$path" "$out.master" "$out.paired" > "$out.diff"; then
      echo "The branch lock is identical to the studio master's lock."
    else
      echo "Diff of the branch lock against the studio master's lock (\`$path\`):"
      echo
      echo '```diff'
      cat "$out.diff"
      echo '```'
    fi
  } >> "${GITHUB_STEP_SUMMARY:-/dev/stdout}"
  mv "$out.paired" "$out"
  rm -f "$out.master" "$out.diff"
else
  [ -z "$branch" ] || echo "No studio branch '$branch' with a lock; using the studio master's lock."
  curl -fsSL "$base/master/$path" -o "$out"
fi
