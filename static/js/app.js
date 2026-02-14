async function downloadPost(postId, caption) {
  const res = await fetch(`/post/${postId}/download`, { method: 'POST' });
  const data = await res.json();
  if (data.ok) {
    const tagEl = document.getElementById(`hashtags-${postId}`);
    if (tagEl) {
      tagEl.textContent = data.hashtags;
      tagEl.classList.remove('d-none');
    }
    alert('Downloaded successfully');
  } else {
    alert(data.error || 'Download failed');
  }
}

async function downloadSelected() {
  const ids = [...document.querySelectorAll('.post-check:checked')].map(i => parseInt(i.value));
  const res = await fetch('/posts/bulk_download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ post_ids: ids }) });
  const data = await res.json();
  alert(`Downloaded ${data.downloaded} posts`);
  location.reload();
}

async function downloadAllUndownloaded() {
  const ids = [...document.querySelectorAll('.post-check')].map(i => parseInt(i.value));
  const res = await fetch('/posts/bulk_download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ post_ids: ids }) });
  const data = await res.json();
  alert(`Downloaded ${data.downloaded} posts`);
  location.reload();
}
