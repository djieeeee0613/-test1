-- 2026-10-28 商家名錄改版：把 Supabase 裡的舊店名改成新名錄的正式店名
-- n8n 用「店名完全一致」查商家 LINE，不改的話這些店家收不到預約通知
-- 可以重複執行（第二次不會再改到任何資料）
BEGIN;
-- merchants.artisan
UPDATE merchants SET artisan = '威石東台灣葡萄酒莊 (Weightstone)' WHERE artisan = '威石東葡萄酒莊（Weightstone）';
UPDATE merchants SET artisan = '臺灣菸酒(股)公司南投觀光酒廠' WHERE artisan = '台灣菸酒南投酒廠';
UPDATE merchants SET artisan = '龍南天然漆博物館（龍南漆器）' WHERE artisan = '龍南天然漆博物館';
UPDATE merchants SET artisan = '毓繡美術館' WHERE artisan = '毓秀美術館';
UPDATE merchants SET artisan = '水里蛇窯陶藝文化園區' WHERE artisan = '水里蛇窯';
UPDATE merchants SET artisan = '大雁休閒農業區' WHERE artisan = '大雁休閒農業區（澀水社區）';
UPDATE merchants SET artisan = '慢午廚房x野食' WHERE artisan = '慢午廚房 X 野食';
UPDATE merchants SET artisan = '雲品溫泉酒店日月潭' WHERE artisan = '雲品溫泉酒店';
UPDATE merchants SET artisan = '溪頭福華渡假飯店-溪頭漢光樓' WHERE artisan = '溪頭福華渡假飯店—漢光樓';
UPDATE merchants SET artisan = '埔里南村里巴宰原鄉文化園區' WHERE artisan = '埔里巴宰原鄉文化園區';
UPDATE merchants SET artisan = '原夢觀光農園' WHERE artisan = '原夢觀光農園（賽德克族文化）';
UPDATE merchants SET artisan = '寶湖宮地母廟' WHERE artisan = '寶湖宮天地堂地母廟';
UPDATE merchants SET artisan = '武岫休閒農場（武岫竹炭窯文化園區）' WHERE artisan = '武岫竹炭窯文化園區';
UPDATE merchants SET artisan = '賽德克喜美傳統織藝工坊' WHERE artisan = '林喜美賽德克織布工作室';
UPDATE merchants SET artisan = '兩腳詩集綠活學旅Footprint Inn' WHERE artisan = '兩腳詩集生活旅店（Footprint Inn）';
-- bookings.artisan
UPDATE bookings SET artisan = '威石東台灣葡萄酒莊 (Weightstone)' WHERE artisan = '威石東葡萄酒莊（Weightstone）';
UPDATE bookings SET artisan = '臺灣菸酒(股)公司南投觀光酒廠' WHERE artisan = '台灣菸酒南投酒廠';
UPDATE bookings SET artisan = '龍南天然漆博物館（龍南漆器）' WHERE artisan = '龍南天然漆博物館';
UPDATE bookings SET artisan = '毓繡美術館' WHERE artisan = '毓秀美術館';
UPDATE bookings SET artisan = '水里蛇窯陶藝文化園區' WHERE artisan = '水里蛇窯';
UPDATE bookings SET artisan = '大雁休閒農業區' WHERE artisan = '大雁休閒農業區（澀水社區）';
UPDATE bookings SET artisan = '慢午廚房x野食' WHERE artisan = '慢午廚房 X 野食';
UPDATE bookings SET artisan = '雲品溫泉酒店日月潭' WHERE artisan = '雲品溫泉酒店';
UPDATE bookings SET artisan = '溪頭福華渡假飯店-溪頭漢光樓' WHERE artisan = '溪頭福華渡假飯店—漢光樓';
UPDATE bookings SET artisan = '埔里南村里巴宰原鄉文化園區' WHERE artisan = '埔里巴宰原鄉文化園區';
UPDATE bookings SET artisan = '原夢觀光農園' WHERE artisan = '原夢觀光農園（賽德克族文化）';
UPDATE bookings SET artisan = '寶湖宮地母廟' WHERE artisan = '寶湖宮天地堂地母廟';
UPDATE bookings SET artisan = '武岫休閒農場（武岫竹炭窯文化園區）' WHERE artisan = '武岫竹炭窯文化園區';
UPDATE bookings SET artisan = '賽德克喜美傳統織藝工坊' WHERE artisan = '林喜美賽德克織布工作室';
UPDATE bookings SET artisan = '兩腳詩集綠活學旅Footprint Inn' WHERE artisan = '兩腳詩集生活旅店（Footprint Inn）';
-- events.artisan
UPDATE events SET artisan = '威石東台灣葡萄酒莊 (Weightstone)' WHERE artisan = '威石東葡萄酒莊（Weightstone）';
UPDATE events SET artisan = '臺灣菸酒(股)公司南投觀光酒廠' WHERE artisan = '台灣菸酒南投酒廠';
UPDATE events SET artisan = '龍南天然漆博物館（龍南漆器）' WHERE artisan = '龍南天然漆博物館';
UPDATE events SET artisan = '毓繡美術館' WHERE artisan = '毓秀美術館';
UPDATE events SET artisan = '水里蛇窯陶藝文化園區' WHERE artisan = '水里蛇窯';
UPDATE events SET artisan = '大雁休閒農業區' WHERE artisan = '大雁休閒農業區（澀水社區）';
UPDATE events SET artisan = '慢午廚房x野食' WHERE artisan = '慢午廚房 X 野食';
UPDATE events SET artisan = '雲品溫泉酒店日月潭' WHERE artisan = '雲品溫泉酒店';
UPDATE events SET artisan = '溪頭福華渡假飯店-溪頭漢光樓' WHERE artisan = '溪頭福華渡假飯店—漢光樓';
UPDATE events SET artisan = '埔里南村里巴宰原鄉文化園區' WHERE artisan = '埔里巴宰原鄉文化園區';
UPDATE events SET artisan = '原夢觀光農園' WHERE artisan = '原夢觀光農園（賽德克族文化）';
UPDATE events SET artisan = '寶湖宮地母廟' WHERE artisan = '寶湖宮天地堂地母廟';
UPDATE events SET artisan = '武岫休閒農場（武岫竹炭窯文化園區）' WHERE artisan = '武岫竹炭窯文化園區';
UPDATE events SET artisan = '賽德克喜美傳統織藝工坊' WHERE artisan = '林喜美賽德克織布工作室';
UPDATE events SET artisan = '兩腳詩集綠活學旅Footprint Inn' WHERE artisan = '兩腳詩集生活旅店（Footprint Inn）';
-- checkins.merchant_name
UPDATE checkins SET merchant_name = '威石東台灣葡萄酒莊 (Weightstone)' WHERE merchant_name = '威石東葡萄酒莊（Weightstone）';
UPDATE checkins SET merchant_name = '臺灣菸酒(股)公司南投觀光酒廠' WHERE merchant_name = '台灣菸酒南投酒廠';
UPDATE checkins SET merchant_name = '龍南天然漆博物館（龍南漆器）' WHERE merchant_name = '龍南天然漆博物館';
UPDATE checkins SET merchant_name = '毓繡美術館' WHERE merchant_name = '毓秀美術館';
UPDATE checkins SET merchant_name = '水里蛇窯陶藝文化園區' WHERE merchant_name = '水里蛇窯';
UPDATE checkins SET merchant_name = '大雁休閒農業區' WHERE merchant_name = '大雁休閒農業區（澀水社區）';
UPDATE checkins SET merchant_name = '慢午廚房x野食' WHERE merchant_name = '慢午廚房 X 野食';
UPDATE checkins SET merchant_name = '雲品溫泉酒店日月潭' WHERE merchant_name = '雲品溫泉酒店';
UPDATE checkins SET merchant_name = '溪頭福華渡假飯店-溪頭漢光樓' WHERE merchant_name = '溪頭福華渡假飯店—漢光樓';
UPDATE checkins SET merchant_name = '埔里南村里巴宰原鄉文化園區' WHERE merchant_name = '埔里巴宰原鄉文化園區';
UPDATE checkins SET merchant_name = '原夢觀光農園' WHERE merchant_name = '原夢觀光農園（賽德克族文化）';
UPDATE checkins SET merchant_name = '寶湖宮地母廟' WHERE merchant_name = '寶湖宮天地堂地母廟';
UPDATE checkins SET merchant_name = '武岫休閒農場（武岫竹炭窯文化園區）' WHERE merchant_name = '武岫竹炭窯文化園區';
UPDATE checkins SET merchant_name = '賽德克喜美傳統織藝工坊' WHERE merchant_name = '林喜美賽德克織布工作室';
UPDATE checkins SET merchant_name = '兩腳詩集綠活學旅Footprint Inn' WHERE merchant_name = '兩腳詩集生活旅店（Footprint Inn）';
-- game_posts.merchant_name
UPDATE game_posts SET merchant_name = '威石東台灣葡萄酒莊 (Weightstone)' WHERE merchant_name = '威石東葡萄酒莊（Weightstone）';
UPDATE game_posts SET merchant_name = '臺灣菸酒(股)公司南投觀光酒廠' WHERE merchant_name = '台灣菸酒南投酒廠';
UPDATE game_posts SET merchant_name = '龍南天然漆博物館（龍南漆器）' WHERE merchant_name = '龍南天然漆博物館';
UPDATE game_posts SET merchant_name = '毓繡美術館' WHERE merchant_name = '毓秀美術館';
UPDATE game_posts SET merchant_name = '水里蛇窯陶藝文化園區' WHERE merchant_name = '水里蛇窯';
UPDATE game_posts SET merchant_name = '大雁休閒農業區' WHERE merchant_name = '大雁休閒農業區（澀水社區）';
UPDATE game_posts SET merchant_name = '慢午廚房x野食' WHERE merchant_name = '慢午廚房 X 野食';
UPDATE game_posts SET merchant_name = '雲品溫泉酒店日月潭' WHERE merchant_name = '雲品溫泉酒店';
UPDATE game_posts SET merchant_name = '溪頭福華渡假飯店-溪頭漢光樓' WHERE merchant_name = '溪頭福華渡假飯店—漢光樓';
UPDATE game_posts SET merchant_name = '埔里南村里巴宰原鄉文化園區' WHERE merchant_name = '埔里巴宰原鄉文化園區';
UPDATE game_posts SET merchant_name = '原夢觀光農園' WHERE merchant_name = '原夢觀光農園（賽德克族文化）';
UPDATE game_posts SET merchant_name = '寶湖宮地母廟' WHERE merchant_name = '寶湖宮天地堂地母廟';
UPDATE game_posts SET merchant_name = '武岫休閒農場（武岫竹炭窯文化園區）' WHERE merchant_name = '武岫竹炭窯文化園區';
UPDATE game_posts SET merchant_name = '賽德克喜美傳統織藝工坊' WHERE merchant_name = '林喜美賽德克織布工作室';
UPDATE game_posts SET merchant_name = '兩腳詩集綠活學旅Footprint Inn' WHERE merchant_name = '兩腳詩集生活旅店（Footprint Inn）';
COMMIT;
