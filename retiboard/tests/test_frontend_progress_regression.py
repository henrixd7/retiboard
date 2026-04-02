
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_resume_seeds_existing_progress_before_starting_placeholder():
    content = (ROOT / "frontend/src/composables/useDecrypt.js").read_text()
    assert "const existingProgress = await fetchPayloadProgress(boardId, attachmentContentHash)" in content
    assert "options.onProgress?.(existingProgress || {" in content


def test_pause_waits_for_backend_pause_before_local_abort():
    thread_content = (ROOT / "frontend/src/composables/useThreadPostAttachments.js").read_text()
    thread_pause_idx = thread_content.index("await pauseAttachmentFetch(boardId(), attachmentHash)")
    thread_abort_idx = thread_content.index("abort?.()")
    assert thread_pause_idx < thread_abort_idx

    catalog_content = (ROOT / "frontend/src/views/CatalogView.vue").read_text()
    catalog_pause_idx = catalog_content.index("await pauseAttachmentFetch(props.boardId, hash)")
    catalog_abort_idx = catalog_content.index("abort?.()")
    assert catalog_pause_idx < catalog_abort_idx


def test_resume_keeps_manual_priority_override():
    assert "await doLoadAttachments(post, true)" in (
        ROOT / "frontend/src/composables/useThreadPostAttachments.js"
    ).read_text()
    assert "doLoadAttachments($event, true)" in (ROOT / "frontend/src/views/CatalogView.vue").read_text()
