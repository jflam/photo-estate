import Foundation

/// Which photos are screenshots, from what the catalog already holds.
///
/// A camera always writes its make and model; a screenshot never has one. So a screenshot is a photo
/// with no camera that is exactly the size of a phone or tablet screen (either way round), or whose
/// name says it is one. Measured on a 113,000-photo library: about 3,000 hits, nearly all PNG.
///
/// Camera photos that lost their EXIF (shared through a chat app, resized by a cloud service) come
/// back at 4:3 or exactly 16:9 (1024 × 768, 1536 × 2048, 720 × 1280...), which are also iPad and old
/// Android screen sizes. A real screenshot from those is a PNG, so for JPEG and HEIC those two shapes
/// don't count.
///
/// Photos on the phone that the server doesn't have yet are told by Photos itself
/// (`PHAssetMediaSubtype.photoScreenshot`), in `LocalPhotos`.
enum Screenshots {
    /// Screen sizes in pixels, short side first.
    static let sizes: [(Int, Int)] = [
        // iPhone
        (320, 480), (640, 960), (640, 1136), (750, 1334), (828, 1792), (1080, 1920), (1242, 2208), (1125, 2436),
        (1242, 2688), (1080, 2340), (1170, 2532), (1284, 2778), (1179, 2556), (1290, 2796), (1206, 2622),
        (1320, 2868), (1260, 2736),
        // iPad
        (768, 1024), (1536, 2048), (1668, 2224), (1668, 2388), (2048, 2732), (1620, 2160), (1640, 2360),
        (1488, 2266), (2064, 2752), (1668, 2420),
        // common Android
        (720, 1280), (1080, 2160), (1080, 2220), (1080, 2280), (1080, 2316), (1080, 2400), (1440, 2560),
        (1440, 2960), (1440, 3040), (1440, 3120), (1440, 3200),
    ]

    /// An SQL condition, over the `photos` table as `p`, true for a screenshot.
    static let sql: String = {
        let keys = sizes.map { String($0.0 * 100_000 + $0.1) }.joined(separator: ",")
        let a = "min(ifnull(p.w,0), ifnull(p.h,0))", b = "max(ifnull(p.w,0), ifnull(p.h,0))"
        return """
            (p.camera IS NULL AND p.lens IS NULL AND (\
            lower(ifnull(p.path,'')) LIKE '%screenshot%' OR lower(ifnull(p.path,'')) LIKE '%screen shot%' OR \
            (\(a) * 100000 + \(b) IN (\(keys)) AND (lower(p.ext) = '.png' OR (\(a) * 4 != \(b) * 3 AND \(a) * 16 != \(b) * 9)))))
            """
    }()
}
