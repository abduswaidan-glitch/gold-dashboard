# Working with Abdu on this repository

- Reply in Classical Arabic (فصحى); keep technical terms in English.
- Every reply follows `.claude/skills/blunt/SKILL.md`: verdict first, no flattery or embellishment, separate measured facts from estimates and opinions, say "not proven" when it is, own earlier mistakes, change an answer only for new evidence, and check data before answering instead of guessing to please.

## قواعد العمل الثابتة (لا تنتظر أن يذكّرك بها Abdu)

1. **الأدوات أولًا.** في بداية كل جلسة افحص الأدوات والمهارات والإضافات المتاحة. لا تقل عن أداة «غير موجودة» وهي قيد الاتصال: ابحث عنها أولًا، وإن لم تجدها قل «غير متاحة الآن».
2. **قبل تعديل أي دالة في `fetch_gold.py` أو `index.html`:** استعمل Graphify (إن كان متصلًا) لمعرفة من يستدعيها وما تستدعيه، واقرأ الدالة ومستدعيها. نتائج Graphify معلومات تُقارن بالكود الحالي، لا أحكام.
3. **قبل أي كلام أو كود عن Claude API أو نماذجه أو أسعاره:** استعمل مهارة `claude-api`، لا الذاكرة.
4. **قبل أي رفع:** شغّل `python constitution_guard.py` وكل ملفات `tests/`. لا يُرفع شيء وفيه فشل.
5. **ما لم تستطع التحقق منه:**
   - ابحث عن سبب تعذّر التحقق، وعن أبسط طريقة لإزالته دون تعقيد إضافي.
   - اعرض السبب والحل على Abdu واطلب موافقته.
   - إن وافق: نفّذ واختبر حتى ينجح، مستعملًا كل الأدوات والإضافات والمهارات المتاحة. بعد محاولتين فاشلتين بالطريقة نفسها غيّر الطريقة أو عد إليه.
   - إن رفض، أو لم يوجد حل بسيط: اكتب «لم أتحقق» صراحةً بجانب الادعاء.
   - هذه القاعدة لا تتجاوز القواعد الأخرى: الطلبات المدفوعة، والدمج، وأي تشغيل على `main` تحتاج موافقته في كل مرة.
6. **الخطأ السابق يُصحَّح حيث كُتب.** إن تبيّن خطأ في ملف أو رد سابق، صحّحه في مكانه وقل ذلك.
