'use client';
import {useCallback, useEffect, useState} from 'react';
import {ArrowRight, Heart, ShoppingBag, Search, Sparkles, Database, Boxes, Cpu, SlidersHorizontal, X} from 'lucide-react';

type Product = {id:string;title:string;description:string;category:string;brand:string;price:number;currency:string;image_url:string;rating:number};
type Result = {items:Product[]; provider?:string; strategy?:string; total?:number};
const fetchJSON = async (path:string, options?:RequestInit) => {
  const response = await fetch(path, {credentials:'same-origin', ...options});
  if(!response.ok) throw new Error(`API error: ${response.status}`);
  return response.json();
};
export default function Home() {
  const [items, setItems] = useState<Product[]>([]);
  const [suggested, setSuggested] = useState<Product[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [category, setCategory] = useState('');
  const [query, setQuery] = useState('');
  const [draft, setDraft] = useState('');
  const [selected, setSelected] = useState<Product|null>(null);
  const [cart, setCart] = useState(0);
  const [provider, setProvider] = useState('local');
  const [strategy, setStrategy] = useState('local');
  const [error, setError] = useState('');
  const reload = useCallback(async () => {
    try {
      const data: Result = query ? await fetchJSON(`/api/search?q=${encodeURIComponent(query)}&category=${encodeURIComponent(category)}`)
        : await fetchJSON(`/api/products?limit=60&category=${encodeURIComponent(category)}`);
      setItems(data.items); setProvider(data.provider || 'database'); setError('');
    } catch {setError('API 尚未连接。请启动 Mac 上的 FastAPI。');}
  }, [query,category]);
  const refreshRecommendations = useCallback(async () => {
    try {const result:Result = await fetchJSON('/api/recommendations/home?limit=8');setSuggested(result.items);setStrategy(result.strategy || 'local');} catch { /* optional */ }
  }, []);
  useEffect(() => {reload();}, [reload]);
  useEffect(() => {refreshRecommendations();fetchJSON('/api/categories').then(setCategories).catch(()=>{});fetchJSON('/api/cart').then(r=>setCart(r.items.reduce((n:number, i:{quantity:number})=>n+i.quantity,0))).catch(()=>{});}, [refreshRecommendations]);
  const emit = async (product:Product, event_type:'click'|'favorite'|'cart') => {
    try {await fetchJSON('/api/events', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:product.id,event_type})});} catch { /* do not block browsing */ }
    refreshRecommendations();
  };
  const addCart = async (p:Product) => {
    try {await fetchJSON('/api/cart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:p.id,quantity:1})});setCart(c=>c+1);refreshRecommendations();}
    catch {setError('购物车操作失败，请检查 API。');}
  };
  const card = (p:Product, i:number) => <article className="product-card" key={p.id}>
    <button className="photo" onClick={()=>{setSelected(p);emit(p,'click');}} aria-label={`查看${p.title}`}>
      {p.image_url ? <img src={p.image_url} alt={p.title} loading="lazy" /> : <span className="product-monogram">{p.category.slice(0,1)}</span>}
      <span className="category-tag">{p.category}</span>
    </button>
    <div className="card-body">
      <span className="brand">{p.brand || 'Marketplace'}</span>
      <button className="product-name" onClick={()=>{setSelected(p);emit(p,'click');}}>{p.title}</button>
      <div className="card-bottom"><strong>{p.currency==='USD'?'$':'¥'}{p.price.toFixed(2)}</strong><button className="add" onClick={()=>addCart(p)} aria-label="加入购物车">+</button></div>
    </div>
  </article>;
  return <main>
    <div className="announcement">DISTRIBUTED COMMERCE LAB <span>·</span> 三节点局域网推荐实验平台 <ArrowRight size={12}/></div>
    <header className="topbar"><div className="logo"><div className="logo-mark">◈</div><span>orbit<span className="logo-thin">market</span></span></div>
      <div className="searchbar"><Search size={19}/><input value={draft} placeholder="搜索商品、品牌、分类..." onChange={e=>setDraft(e.target.value)} onKeyDown={e=>e.key==='Enter'&&setQuery(draft.trim())}/><button onClick={()=>setQuery(draft.trim())}>搜索</button></div>
      <div className="header-actions"><button title="展示收藏功能" onClick={()=>document.getElementById('catalog')?.scrollIntoView({behavior:'smooth'})}><Heart size={21}/></button><button className="cart-pill" title="演示购物车"><ShoppingBag size={20}/><span>{cart}</span></button></div>
    </header>
    <section className="hero"><div><div className="eyebrow"><Sparkles size={14}/> INTELLIGENT DISCOVERY</div><h1>发现真正<br/><em>适合你的</em> 好物。</h1><p>连接搜索、发现与偏好的新一代商品实验商城。由三台计算机共同驱动的个性化探索之旅。</p><button className="hero-btn" onClick={()=>document.getElementById('catalog')?.scrollIntoView({behavior:'smooth'})}>探索商品 <ArrowRight size={18}/></button></div>
      <div className="hero-art"><div className="hero-orb">◈</div><span className="art-label">INTELLIGENCE IN MOTION</span></div></section>
    <section className="cluster"><div><Database size={18}/><span>Ubuntu A · 数据与检索</span></div><div><Cpu size={18}/><span>Ubuntu B · 推荐计算</span></div><div><Boxes size={18}/><span>macOS · 网页与 API</span></div><span className="status-label">● LAN LAB</span></section>
    <section className="section" id="catalog"><div className="section-heading"><div><span className="overline">CURATED CATALOG</span><h2>{query ? `搜索结果：${query}` : '探索精选商品'}</h2><p>从真实数据集或本地演示数据中发现商品 · 搜索来源：{provider}</p></div><div className="filter-label"><SlidersHorizontal size={16}/> 精选集合</div></div>
      <div className="chips"><button className={!category?'active':''} onClick={()=>setCategory('')}>全部商品</button>{categories.map(c=><button key={c} className={c===category?'active':''} onClick={()=>setCategory(c)}>{c}</button>)}</div>
      {error && <p className="alert">{error}</p>}
      <div className="catalog-grid">{items.map(card)}</div>{items.length===0&&!error&&<p className="empty">暂无商品。首次部署请执行示例数据导入。</p>}
    </section>
    <section className="section rec-section"><div className="section-heading"><div><span className="overline">JUST FOR YOU</span><h2>猜你喜欢 <span className="stars">✳</span></h2><p>根据你的浏览偏好动态调整 · 策略：{strategy}</p></div><button className="subtle-btn" onClick={refreshRecommendations}>更新推荐 <ArrowRight size={16}/></button></div><div className="catalog-grid">{suggested.map(card)}</div></section>
    <footer><span>◈ orbitmarket</span><span>Research demonstration · Prices and stock are not live offers · 2026</span></footer>
    {selected&&<div className="modal-backdrop" onClick={()=>setSelected(null)}><div className="modal" role="dialog" aria-modal="true" aria-label={selected.title} onClick={e=>e.stopPropagation()}><button className="close" onClick={()=>setSelected(null)}><X/></button><div className="modal-photo">{selected.image_url?<img src={selected.image_url} alt=""/>:<span className="product-monogram">{selected.category.slice(0,1)}</span>}</div><div className="modal-copy"><span className="overline">{selected.category} · {selected.brand}</span><h2>{selected.title}</h2><p>{selected.description||'暂无详细描述。'}</p><div className="modal-price">{selected.currency==='USD'?'$':'¥'}{selected.price.toFixed(2)}</div><button className="hero-btn" onClick={()=>{addCart(selected);setSelected(null);}}>加入演示购物车 <ShoppingBag size={18}/></button><small>仅用于研究展示，不接受真实交易。</small></div></div></div>}
  </main>;
}
