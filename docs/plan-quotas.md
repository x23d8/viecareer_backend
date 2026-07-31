# Hạn mức sử dụng các gói VieCareer

Chu kỳ ngày và tuần dùng múi giờ `Asia/Ho_Chi_Minh`. Hạn mức ngày đặt lại lúc
00:00 mỗi ngày; hạn mức tuần đặt lại lúc 00:00 thứ Hai.

| Tính năng | Free | Standard | Premium | Team (mỗi thành viên) |
|---|---:|---:|---:|---:|
| Phân tích CV & JD | 3 trọn đời | 15 / tuần | 30 / tuần | 30 / tuần |
| Phỏng vấn AI | 3 trọn đời | 10 / tuần | 20 / tuần | 20 / tuần |
| Thời lượng mỗi phỏng vấn | 10 phút | 25 phút | 45 phút | 45 phút |
| Tin nhắn VieBot | 3 trọn đời | 50 / ngày | 150 / ngày | 150 / ngày |
| Tạo đề coding | 3 trọn đời | 10 / tuần | 15 / tuần | 15 / tuần |
| Chấm code bằng AI | 3 trọn đời | 10 / tuần | 15 / tuần | 15 / tuần |

## Cách trừ lượt

- Free: ba lượt được tính riêng cho từng tính năng, gắn với tài khoản và không có
  `reset_at`. Đăng xuất, gọi lại API, hết tháng hoặc hết tuần không phục hồi lượt.
- Tài khoản tồn tại trước ngày áp dụng chính sách được cấp một mốc dùng thử mới
  đúng một lần, vì vậy mỗi tài khoản cũ nhận lại đủ 3 lượt cho từng tính năng Free. Mốc này
  được lưu ở database và không thay đổi khi deploy lại hoặc gọi API.
- Phỏng vấn AI: trừ ngay khi backend tạo phiên. Hủy phiên, đóng trình duyệt hoặc
  mất kết nối không hoàn lượt.
- Phân tích CV/JD, VieBot, tạo đề coding và chấm code: tính một lượt khi tác vụ
  hoàn tất thành công.
- Gói mua lẻ 29.000 VND cộng một lượt phỏng vấn tối đa 15 phút. Lượt mua thêm
  không hết hạn và không hoàn lại sau khi phiên bắt đầu.
- Khi mua gói giữa chu kỳ, người dùng nhận đủ hạn mức cho phần còn lại của ngày
  hoặc tuần; lần đặt lại kế tiếp vẫn theo lịch ở trên.

API `GET /api/billing/plans` trả từng mục trong `limits` với `label`,
`unit_label`, `period_label`, `reset_policy` và `consumption_rule` để giao diện
pricing/quota không phải tự suy diễn quy tắc.

## Quyền quản trị viên

Tài khoản đang hoạt động có role `admin` trong database được bỏ qua toàn bộ hạn
mức sản phẩm: phân tích CV/JD, VieBot, tạo đề coding, chấm code, số lượt và thời
lượng phỏng vấn, cùng giới hạn AI theo ngày. API billing trả
`unlimited_access: true`, `limit: null`, `remaining: null` và
`period: "unlimited"` cho các tài khoản này.

Các giới hạn an toàn vận hành như chống spam theo IP/phút và số tác vụ AI chạy
đồng thời vẫn áp dụng để một tài khoản không thể làm nghẽn dịch vụ.
