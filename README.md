# Claude Organize

Claude Code ile bir sürü sohbet ve fork açınca nerede ne yaptığını unutmamak için küçük bir araç.

Claude Code bütün sohbetleri `~/.claude/projects/<proje>/<session-id>.jsonl` dosyalarında tutar.
Bu araç o dosyaları **sadece okur** (hiçbir şeyi değiştirmez) ve sana şunları gösterir:

- Sohbetler **proje klasörüne göre gruplu**, **fork'lar ana sohbetin altında ağaç olarak**
- Fork'lar, fork'tan sonraki ilk isteğinle adlandırılır ("Deneme: Logo-Animation V4" gibi), böylece kardeşleri ayırt edebilirsin
- Son çalışma zamanı, git branch, ilk/son istek, Claude'un son cevabı, dokunulan dosyalar
- Her sohbete kendi **notun** + **durum** etiketi: 🟢 aktif · 🧪 deneme · ⏸️ beklemede · ✅ bitti · 🗑️ çöp
- Tek tıkla kopyalanan `cd "<klasör>" && claude --resume <id>` komutu

Notlar `~/.claude-organize/notes.json` dosyasında durur.

## Kurulum

Python 3.8+ yeterli, başka bir şey kurman gerekmez.

```bash
git clone <bu repo> ~/claude-organize
```

İstersen kısa bir takma ad tanımla (`~/.zshrc` dosyasına ekle):

```bash
alias co="python3 ~/claude-organize/claude_organize.py"
```

## Kullanım

```bash
co                      # web arayüzü açılır: http://127.0.0.1:8765
co nerdeyim             # terminalde son 5 sohbetin özeti
co nerdeyim --here      # sadece bulunduğun klasördeki sohbetler
co list                 # tüm açık işler, fork ağacıyla (-f detaylı, -a bitti/çöp dahil)
co note "V4'te easing deniyorum, sırada renkler" -s deneme   # bu klasördeki en son sohbete not
co note 3f2a "bunu bıraktım" -s cop                           # id'nin başıyla belirli sohbete
```

### Claude Code içinden `/nerdeyim`

```bash
co install-command
```

Bu, `~/.claude/commands/nerdeyim.md` dosyasını oluşturur. Artık herhangi bir sohbette
`/nerdeyim` yazınca Claude o sohbetin **amaç / şu anki durum / sıradaki adım** özetini çıkarır
ve not olarak kaydeder. Bir şeyi bırakıp başka sohbete geçmeden önce yazman yeterli;
geri döndüğünde arayüzde o not seni bekliyor olur.

## Önerilen alışkanlık

1. Fork açtığında ilk mesajı "Deneme: …" diye başlat. Ağaçta o isimle görünür.
2. Sohbetten çıkmadan önce `/nerdeyim` yaz.
3. Ne yapacağını unuttuğunda `co` yazıp arayüze bak: en son dokunulan sohbetin turuncu çizgisi olur.

## Not

Claude masaüstü uygulamasının kenar çubuğunda gördüğün isimler (örn. "03-Logo animation (fork)")
uygulamanın kendi verisinde durabilir. Bu araç Claude Code'un oturum dosyalarını okur;
`/rename` ile verdiğin başlıklar ve otomatik özetler varsa onları kullanır, yoksa ilk isteğini başlık yapar.
İstediğin sohbete arayüzden "kısa isim" verebilirsin.
