"""xargs and parallel count as writers only when the command they run can write: a read-only pipeline such as
`find . | xargs wc -l` passes in the profile folder as it did in the released guard, while `| xargs rm` asks."""
import pytest

from fb_fakes import sub

approval = sub("approval")


@pytest.mark.parametrize("command", [
    "find . -name '*.py' | xargs wc -l",
    "find . -name '*.py' | xargs grep -l TODO",
    "git ls-files | xargs -n 1 grep -c import",
    "find ~ -maxdepth 1 | xargs ls -d",
    "ls | xargs -I{} echo {}",
    "find . -type f | xargs -0 -P 4 cat",
    "ls | xargs",
])
def test_xargs_running_a_reader_replaces_nothing(command):
    replaced, here = approval._replaced(command)
    assert replaced == [] and here is False


@pytest.mark.parametrize("command", [
    "ls .env | xargs rm",
    "find . -name config.yaml | xargs -I{} cp evil {}",
    "find . | xargs -n 1 sed -i s/a/b/",
    "ls | parallel rm",
    "ls | xargs sh -c 'rm $0'",
])
def test_xargs_running_a_writer_still_counts_every_word(command):
    replaced, _ = approval._replaced(command)
    assert replaced, command
