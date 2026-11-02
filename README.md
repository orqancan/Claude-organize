# Claude Organize

Claude Code ile bir sürü sohbet ve fork açınca nerede ne yaptığını unutmamak için küçük bir araç.

Claude Code bütün sohbetleri `~/.claude/projects/<proje>/<session-id>.jsonl` dosyalarında tutar.
Bu araç o dosyaları **sadece okur** (hiçbir şeyi değiştirmez) ve sana şunları gösterir:

Ekran sade bir liste — her sohbet tek satır:

```
WEIHNACHTSFEIER
 ●  Animationen und Bühnenkonzept  çalışıyor…  📝 sahne ışıkları   ▬▬▬─ %62   12 dk · 14:32
 ●  02-Key Visual Animation                                       ▬─── %18   Pzt 10:05
      └ btw: niye takıldı                                          ▬▬── %41   28 Eki
```

- **●** turuncu = son 1 saatte kullanıldı, yanıp sönüyorsa Claude **şu an çalışıyor**
- **Fork'lar** ana sohbetin altında girintili
- **%** = sohbetin hafızasının (context) ne kadar dolduğu; %80'i geçince kırmızı olur, yakında sıkışır demektir
- **Saat / gün**: bugünse saat, bu haftaysa gün adı, daha eskiyse tarih
- Satıra **tıkla** → `cd "<klasör>" && claude --resume <id>` kopyalanır, Terminal'e yapıştır
- **✎** → not yaz veya **✓ bitti** olarak işaretle (bitenler en altta gizli durur)

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
