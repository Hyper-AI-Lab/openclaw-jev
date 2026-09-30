"""Structure-aware chunking: the table of contents, chunk sizes, overlap, and no text lost."""
import re

from app.deep_memory import chunking as ch

GUIDE = """Kubernetes on a single VPS, in three parts.

# Kubernetes guide

## Install k3s

Run the installer and check the node.

```bash
# not a heading: a shell comment
curl -sfL https://get.k3s.io | sh -

kubectl get nodes
```

## Networking

### Ingress

Traefik ships with k3s. Point your DNS at the VPS.

### Certificates

Use cert-manager with a ClusterIssuer for Let's Encrypt.

## Empty heading

## Storage
- local-path provisioner by default
- Longhorn for replicas
"""


def test_headings_become_a_table_of_contents_with_paths():
    sections = ch.split_sections(GUIDE, root_title="Kubernetes on a VPS")
    assert [(s.ordinal, s.title, s.path, s.level) for s in sections] == [
        (0, "Kubernetes on a VPS", "Kubernetes on a VPS", 1),
        (1, "Install k3s", "Kubernetes guide > Install k3s", 2),
        (2, "Ingress", "Kubernetes guide > Networking > Ingress", 3),
        (3, "Certificates", "Kubernetes guide > Networking > Certificates", 3),
        (4, "Storage", "Kubernetes guide > Storage", 2),
    ]
    install = sections[1].text
    assert "# not a heading: a shell comment" in install and install.endswith("```")
    assert ch.toc_entries(sections)[2] == {"ordinal": 2, "title": "Ingress",
                                           "path": "Kubernetes guide > Networking > Ingress", "level": 3}


def test_setext_headings_count_but_a_thematic_break_does_not():
    text = "Overview\n========\n\nIntro words.\n\n---\n\nStill intro.\n\nDetails\n-------\nThe details."
    sections = ch.split_sections(text)
    assert [(s.title, s.level) for s in sections] == [("Overview", 1), ("Details", 2)]
    assert "---" in sections[0].text and "Still intro." in sections[0].text


def test_heading_titles_are_cleaned_and_text_without_headings_is_one_section():
    sections = ch.split_sections("## **Step 1: Prepare:** ##\nDo it.")
    assert sections[0].title == "Step 1: Prepare"
    page = ("## Release Historyhttps://kubernetes.io/releases/#release-history\nOne.\n\n"
            "## Release Channels[](https://docs.k3s.io/upgrades/manual#release-channels)\nTwo.\n\n"
            "## See [the docs](https://docs.k3s.io) first\nThree.")
    assert [s.title for s in ch.split_sections(page)] == ["Release History", "Release Channels", "See the docs first"]
    assert ch.split_sections("Just a note.") == [ch.SectionSpec(0, "Content", "Content", 1, "Just a note.")]
    assert ch.split_sections("Just a note.", root_title="Note")[0].path == "Note"
    assert ch.split_sections("   \n\n  ") == []


def test_normalize_unifies_line_endings_and_blank_runs():
    assert ch.normalize("a  \r\nb\r\r\n\n\n\nc\n") == "a\nb\n\nc"


def test_blocks_are_packed_greedily_up_to_the_target():
    paragraphs = [f"Paragraph {i} " + "word " * 60 for i in range(10)]
    chunks = ch.chunk_text("\n\n".join(paragraphs), overlap=0)
    assert len(chunks) > 1 and all(len(c) <= ch.TARGET_CHARS for c in chunks)
    # Nothing that fit was left out of a chunk: each next paragraph would have overflowed it.
    assert all(len(a) + 2 + len(b.split("\n\n")[0]) > ch.TARGET_CHARS for a, b in zip(chunks, chunks[1:]))


def test_an_oversized_paragraph_splits_at_sentences_then_hard_at_words():
    sentences = " ".join(f"Sentence {i} explains one more detail of the plan." for i in range(80))
    chunks = ch.chunk_text(sentences, overlap=0)
    assert len(chunks) > 1 and all(len(c) <= ch.MAX_CHARS for c in chunks)
    assert all(c.rstrip().endswith(".") for c in chunks[:-1])
    run = "x" * 5000
    hard = ch.chunk_text(run, overlap=0)
    assert [len(c) for c in hard] == [1800, 1800, 1400] and "".join(hard) == run


def test_a_tiny_tail_is_merged_into_the_previous_chunk():
    text = "\n\n".join(["alpha " * 200, "beta " * 30])
    chunks = ch.chunk_text(text, overlap=0)
    assert len(chunks) == 1 and chunks[0].endswith("beta")


def test_overlap_starts_on_a_word_boundary_and_is_bounded():
    text = "\n\n".join(f"Block {i}: " + " ".join(f"token{i}x{j}" for j in range(120)) for i in range(4))
    plain = ch.chunk_text(text, overlap=0)
    overlapped = ch.chunk_text(text)
    assert overlapped[0] == plain[0] and len(overlapped) == len(plain)
    for prev, own, chunk in zip(plain, plain[1:], overlapped[1:]):
        prefix = chunk[: len(chunk) - len(own)].strip()
        assert chunk.endswith(own) and prefix and prev.endswith(prefix)
        assert re.match(r"token\d+x\d+", prefix) and len(prefix) <= ch.OVERLAP_CHARS
        assert len(chunk) <= ch.MAX_CHARS + ch.OVERLAP_CHARS + 2


def test_a_fenced_code_block_stays_whole_when_it_fits():
    code = "```python\n" + "\n\n".join(f"def f{i}():\n    return {i}" for i in range(20)) + "\n```"
    text = "Intro paragraph.\n\n" + code + "\n\nAfter the code."
    chunks = ch.chunk_text(text, overlap=0)
    assert any(code in c for c in chunks)


def test_documents_number_chunks_globally_and_link_them_to_sections():
    sections, chunks = ch.chunk_document(GUIDE)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert {c.section_ordinal for c in chunks} == {s.ordinal for s in sections}
    assert ch.chunk_document("") == ([], [])


def test_turns_stay_whole_unless_long():
    assert ch.chunk_turn("  Kirill: my test code word is PELICAN-47.  ") == ["Kirill: my test code word is PELICAN-47."]
    long_turn = "Aura: " + " ".join(f"Point {i} of the answer is here." for i in range(200))
    assert len(ch.chunk_turn(long_turn)) > 1 and ch.chunk_turn("") == []


def test_chunking_is_deterministic_and_loses_no_words():
    text = GUIDE * 3 + "\n\n" + " ".join(f"Closing sentence {i}." for i in range(300))
    first, second = ch.chunk_document(text), ch.chunk_document(text)
    assert first == second
    sections, chunks = first
    for section in sections:
        own = [c.text for c in chunks if c.section_ordinal == section.ordinal]
        plain = ch.chunk_text(section.text, overlap=0)
        assert len(own) == len(plain)
        assert " ".join(plain).split() == ch.normalize(section.text).split()
