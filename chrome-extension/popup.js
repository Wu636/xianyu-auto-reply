const serverInput = document.getElementById('server');
const tokenInput = document.getElementById('token');
const captureButton = document.getElementById('capture');
const statusBox = document.getElementById('status');

function setStatus(text, kind = '') {
  statusBox.textContent = text;
  statusBox.className = `status ${kind}`.trim();
}

chrome.storage.local.get(['contentProductServer', 'contentProductToken'], data => {
  if (data.contentProductServer) serverInput.value = data.contentProductServer;
  if (data.contentProductToken) tokenInput.value = data.contentProductToken;
});

function collectXPage() {
  const clean = value => String(value || '').replace(/\u00a0/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  const canonicalImageUrl = value => {
    try {
      const url = new URL(String(value || ''), location.href);
      if (!/^https?:$/.test(url.protocol)) return '';
      // X commonly stores the real image type in `format=jpg/png/webp`.
      // Removing the complete query string creates a URL which looks valid
      // but returns an HTML/error response when the popup downloads it.
      if (/^(?:pbs|ton)\.twimg\.com$/i.test(url.hostname)) {
        url.searchParams.delete('name');
        url.searchParams.delete('tag');
      }
      return url.toString();
    } catch (_error) {
      return '';
    }
  };
  const isContentImageUrl = value => {
    try {
      const url = new URL(String(value || ''), location.href);
      if (/^ton\.twimg\.com$/i.test(url.hostname)) return true;
      if (!/^pbs\.twimg\.com$/i.test(url.hostname)) return false;
      return !/(?:^|\/)(?:profile_images|profile_banners|emoji|hashflags)(?:\/|$)/i.test(url.pathname);
    } catch (_error) {
      return false;
    }
  };
  const srcsetUrls = value => String(value || '').split(',')
    .map(item => item.trim().split(/\s+/)[0])
    .filter(Boolean);
  const imageSource = node => {
    if (!node) return '';
    const picture = node.closest?.('picture');
    const candidates = [
      node.currentSrc,
      node.getAttribute?.('src'),
      node.getAttribute?.('data-src'),
      ...srcsetUrls(node.getAttribute?.('srcset')),
      ...[...(picture?.querySelectorAll?.('source') || [])].flatMap(source => srcsetUrls(source.getAttribute('srcset')))
    ];
    return candidates.map(canonicalImageUrl).find(isContentImageUrl) || '';
  };
  const contentImages = root => [...(root?.querySelectorAll?.('img') || [])]
    .map(img => ({node: img, url: imageSource(img), alt: clean(img.alt)}))
    .filter(item => item.url);
  const inlineMarkdown = node => {
    if (!node) return '';
    if (node.nodeType === Node.TEXT_NODE) return String(node.nodeValue || '').replace(/\s+/g, ' ');
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const tag = node.tagName.toLowerCase();
    if (['script', 'style', 'svg', 'button', 'img'].includes(tag)) return '';
    if (tag === 'br') return '\n';
    const content = [...node.childNodes].map(inlineMarkdown).join('').replace(/[ \t]+/g, ' ').trim();
    if (!content) return '';
    if (tag === 'strong' || tag === 'b') return `**${content}**`;
    if (tag === 'em' || tag === 'i') return `*${content}*`;
    if (tag === 'code') return `\`${content.replace(/`/g, '′')}\``;
    if (tag === 'a' && /^https?:/i.test(node.href || '')) return `[${content}](${node.href})`;
    return content;
  };
  const collectRichBlocks = root => {
    if (!root) return [];
    const selector = 'h1,h2,h3,h4,h5,h6,p,li,blockquote,pre,img,[dir="auto"]';
    const candidates = [...root.querySelectorAll(selector)].filter(node => {
      if (node.tagName.toLowerCase() === 'img') return Boolean(imageSource(node));
      const text = clean(node.innerText || node.textContent);
      if (!text) return false;
      // X uses nested dir=auto wrappers. Keep the deepest meaningful block so
      // the same paragraph is never emitted twice.
      const nested = [...node.querySelectorAll(selector)].filter(child => child !== node && child.tagName.toLowerCase() !== 'img');
      return !nested.some(child => clean(child.innerText || child.textContent) === text);
    });
    const seenText = new Set();
    const seenImages = new Set();
    const blocks = [];
    for (const node of candidates) {
      const tag = node.tagName.toLowerCase();
      if (tag === 'img') {
        const sourceUrl = imageSource(node);
        if (sourceUrl && !seenImages.has(sourceUrl)) {
          seenImages.add(sourceUrl);
          blocks.push({type: 'image', source_url: sourceUrl, alt: clean(node.alt) || '原文配图'});
        }
        continue;
      }
      const markdown = clean(inlineMarkdown(node) || node.innerText || node.textContent);
      if (!markdown || seenText.has(markdown)) continue;
      seenText.add(markdown);
      if (/^h[1-6]$/.test(tag)) {
        blocks.push({type: 'heading', level: Number(tag.slice(1)), markdown});
      } else if (tag === 'li') {
        const parentTag = node.parentElement?.tagName?.toLowerCase();
        blocks.push({type: 'list_item', ordered: parentTag === 'ol', markdown});
      } else if (tag === 'blockquote') {
        blocks.push({type: 'quote', markdown});
      } else if (tag === 'pre') {
        blocks.push({type: 'code', markdown});
      } else {
        const style = getComputedStyle(node);
        const fontSize = parseFloat(style.fontSize || '0');
        const fontWeight = parseInt(style.fontWeight || '400', 10);
        if (markdown.length <= 120 && (fontSize >= 20 || fontWeight >= 650)) {
          blocks.push({type: 'heading', level: fontSize >= 26 ? 1 : 2, markdown});
        } else {
          blocks.push({type: 'paragraph', markdown});
        }
      }
    }
    return blocks;
  };
  const longformRoots = [...document.querySelectorAll([
    '[data-testid="twitterArticleReadView"]',
    '[data-testid="longformRichTextComponent"]',
    '[data-testid="twitterArticleRichTextView"]'
  ].join(','))];
  const longformRoot = longformRoots
    .map(node => ({node, text: clean(node.innerText)}))
    .filter(item => item.text.length >= 80)
    .sort((a, b) => b.text.length - a.text.length)[0];
  // `twitterArticleRichTextView` sometimes contains only text while Article
  // images live in the surrounding read view. Prefer the outer read view for
  // ordered blocks and image discovery, but keep the text-only node as a
  // fallback for older X layouts.
  const articleReadRoot = document.querySelector('[data-testid="twitterArticleReadView"]');
  const longformContentRoot = articleReadRoot || longformRoot?.node || null;
  const articles = [...document.querySelectorAll('article[data-testid="tweet"]')];
  const currentStatus = location.pathname.match(/^\/([^/]+)\/status\/(\d+)/i);
  const currentHandle = currentStatus ? currentStatus[1].toLowerCase() : '';
  const posts = [];
  const seen = new Set();
  for (const article of articles) {
    const text = clean([...article.querySelectorAll('[data-testid="tweetText"]')].map(node => node.innerText).join('\n'));
    if (!text) continue;
    const time = article.querySelector('time');
    const link = time?.closest('a')?.href || '';
    const match = link.match(/^https?:\/\/(?:x|twitter)\.com\/([^/]+)\/status\/(\d+)/i);
    const handle = match ? match[1].toLowerCase() : '';
    if (currentHandle && handle && handle !== currentHandle) continue;
    const key = link || text;
    if (seen.has(key)) continue;
    seen.add(key);
    const imageUrls = contentImages(article)
      .map(item => ({url: item.url, alt: item.alt}));
    posts.push({text, url: link, datetime: time?.dateTime || '', handle, images: imageUrls});
  }
  const longformImages = contentImages(longformContentRoot)
    .map(item => ({url: item.url, alt: item.alt}));
  let structuredBlocks = longformContentRoot ? collectRichBlocks(longformContentRoot) : [];
  if (!structuredBlocks.length) {
    structuredBlocks = posts.flatMap((post, index) => [
      ...(posts.length > 1 ? [{type: 'heading', level: 2, markdown: `线程 ${index + 1}`}] : []),
      ...post.text.split(/\n{2,}|\n/).map(text => ({type: 'paragraph', markdown: clean(text)})).filter(item => item.markdown),
      ...(post.images || []).map(image => ({
        type: 'image', source_url: canonicalImageUrl(image.url), alt: image.alt || '线程配图'
      }))
    ]);
  }
  const blockImages = structuredBlocks.filter(block => block.type === 'image').map(block => ({
    url: block.source_url, alt: block.alt || ''
  }));
  const allImages = [...blockImages, ...longformImages, ...posts.flatMap(post => post.images || [])];
  const threadText = posts.map((post, index) => `${posts.length > 1 ? `## 线程 ${index + 1}\n\n` : ''}${post.text}`).join('\n\n');
  const sourceText = longformRoot && longformRoot.text.length > threadText.length
    ? longformRoot.text
    : threadText;
  const pageHeading = clean(longformContentRoot?.querySelector('h1')?.innerText || document.querySelector('main h1')?.innerText);
  return {
    source_url: location.href,
    source_title: pageHeading || clean(document.title.replace(/\s*\/\s*X$/, '')) || 'X 长文',
    source_text: sourceText,
    structured_blocks: structuredBlocks,
    author: currentHandle,
    posts,
    image_urls: [...new Map(allImages.map(item => [canonicalImageUrl(item.url), {...item, url: canonicalImageUrl(item.url)}])).values()].slice(0, 8),
    page_language: document.documentElement.lang || '',
    captured_at: new Date().toISOString()
  };
}

async function imageToDataUrl(item) {
  const url = new URL(item.url);
  if (/^pbs\.twimg\.com$/i.test(url.hostname)) url.searchParams.set('name', 'orig');
  const response = await fetch(url.toString(), {credentials: 'omit'});
  if (!response.ok) throw new Error(`图片下载 HTTP ${response.status}`);
  const contentType = String(response.headers.get('content-type') || '').toLowerCase();
  if (!contentType.startsWith('image/')) throw new Error(`图片地址返回了 ${contentType || '非图片内容'}`);
  const blob = await response.blob();
  if (blob.size > 8 * 1024 * 1024) throw new Error('单张图片超过 8MB');
  const data = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
  const format = String(url.searchParams.get('format') || '').replace(/[^a-z0-9]/gi, '').toLowerCase();
  const mimeExtension = contentType.split(';')[0].split('/')[1]?.replace('jpeg', 'jpg').replace(/[^a-z0-9]/gi, '');
  const pathName = url.pathname.split('/').pop() || 'x-image';
  const filename = /\.[a-z0-9]{2,5}$/i.test(pathName)
    ? pathName
    : `${pathName}.${format || mimeExtension || 'jpg'}`;
  return {
    filename,
    alt: item.alt || '',
    source_url: item.url,
    role: 'source',
    data
  };
}

captureButton.addEventListener('click', async () => {
  const server = serverInput.value.trim().replace(/\/$/, '');
  const token = tokenInput.value.trim();
  if (!server || !token) return setStatus('请先填写本地服务地址和配对码', 'error');
  captureButton.disabled = true;
  setStatus('正在读取页面…');
  try {
    await chrome.storage.local.set({contentProductServer: server, contentProductToken: token});
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (!tab?.url || !/^https?:\/\/(x|twitter)\.com\//i.test(tab.url)) throw new Error('请先打开一个 X 帖子或长文页面');
    const [{result: payload}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: collectXPage});
    if (!payload?.source_text || payload.source_text.length < 20) throw new Error('当前页面没有读到正文，请先展开长文并向下滚动加载线程');
    const detectedImageCount = (payload.image_urls || []).length;
    setStatus(`已读取正文和 ${payload.posts.length} 段线程，发现 ${detectedImageCount} 张配图，正在保存…`);
    payload.images = [];
    const imageFailures = [];
    for (const item of payload.image_urls || []) {
      try {
        payload.images.push(await imageToDataUrl(item));
      } catch (error) {
        imageFailures.push(error.message || String(error));
        console.warn('X 图片保存失败', item.url, error);
      }
    }
    delete payload.image_urls;
    if (detectedImageCount && !payload.images.length) {
      throw new Error(`发现 ${detectedImageCount} 张配图，但下载均失败：${imageFailures[0] || '图片地址不可用'}`);
    }
    const response = await fetch(`${server}/content-products/browser-import`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Content-Import-Token': token},
      body: JSON.stringify(payload)
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `本地服务 HTTP ${response.status}`);
    const failureText = imageFailures.length ? `，另有 ${imageFailures.length} 张下载失败` : '';
    setStatus(`采集完成：正文/线程 ${payload.posts.length || 1} 段、${payload.images.length} 张图片${failureText}。回到系统点击“刷新采集”。`, 'ok');
  } catch (error) {
    setStatus(error.message || String(error), 'error');
  } finally {
    captureButton.disabled = false;
  }
});
