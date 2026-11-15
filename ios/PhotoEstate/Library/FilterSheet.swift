import SwiftUI

/// The sheet behind a filter chip: the values of a facet with live counts, the trips, or the years.
/// Counts come from the phone's catalog and follow every other filter, so a value that would give
/// nothing shows as zero rather than vanishing.
struct FilterSheet: View {
    let pill: Pill
    @ObservedObject var model: LibraryModel
    @Environment(\.dismiss) private var dismiss
    @State private var facet = Facet.country
    @State private var counts: [(value: String, count: Int)] = []
    @State private var months: [(ym: Int, count: Int)] = []
    @State private var filter = ""
    @State private var open: Int?

    var body: some View {
        NavigationStack {
            Group {
                switch pill {
                case .dates: years
                case .trips: tripList
                default: values
                }
            }
            .navigationTitle(pill.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    if pill.value(model.query, trips: model.trips) != nil { Button("Clear") { model.query = pill.cleared(model.query) } }
                }
                ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() }.fontWeight(.semibold) }
            }
        }
        .tint(.sun)
        .task(id: Key(facet: facet, query: model.query)) { await load() }
        .onAppear { facet = pill.facets.first ?? .country }
    }

    private struct Key: Equatable {
        let facet: Facet
        let query: Query
    }

    private func load() async {
        let plan = Reader.plan(model.query, model.vocab)
        if pill == .dates { months = await Catalog.shared.months(plan) }
        else { counts = await Catalog.shared.counts(pill == .trips ? .trip : facet, plan) }
    }

    // MARK: a facet's values

    private var values: some View {
        let picked = model.query.picked(facet)
        let found = Dictionary(counts.map { ($0.value, $0.count) }, uniquingKeysWith: { a, _ in a })
        // what is chosen stays listed even when the other filters leave it empty
        let rows = picked.filter { found[$0] == nil }.map { (value: $0, count: 0) } + counts
        let shown = rows.filter { filter.isEmpty || $0.value.localizedCaseInsensitiveContains(filter) }.prefix(400)
        return List {
            if pill.facets.count > 1 {
                Picker("", selection: $facet) {
                    ForEach(pill.facets) { f in Text(f == .city ? "Places" : f == .country ? "Countries" : f == .region ? "Regions" : f == .camera ? "Cameras" : "Lenses").tag(f) }
                }
                .pickerStyle(.segmented)
                .listRowSeparator(.hidden)
            }
            ForEach(Array(shown), id: \.value) { row in
                Button { Haptics.tick(); model.toggle(facet, row.value) } label: {
                    HStack {
                        let parts = row.value.split(separator: ",", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
                        VStack(alignment: .leading, spacing: 1) {
                            Text(pill == .place ? parts.first ?? row.value : row.value).font(.serif(17, .medium)).foregroundStyle(.primary)
                            if pill == .place, parts.count > 1 { Text(parts[1]).font(.footnote).foregroundStyle(.secondary) }
                        }
                        Spacer()
                        Text(row.count.formatted()).foregroundStyle(.secondary).monospacedDigit()
                        Image(systemName: "checkmark").fontWeight(.bold).foregroundStyle(Color.sun).opacity(picked.contains(row.value) ? 1 : 0)
                    }
                    .opacity(row.count == 0 ? 0.5 : 1)
                }
            }
        }
        .listStyle(.plain)
        .searchable(text: $filter, placement: .navigationBarDrawer(displayMode: .always), prompt: "Filter")
        .overlay { if counts.isEmpty && picked.isEmpty { ContentUnavailableView("None in these photos", systemImage: "line.3.horizontal.decrease.circle") } }
    }

    // MARK: trips

    private var tripList: some View {
        let found = Dictionary(counts.map { ($0.value, $0.count) }, uniquingKeysWith: { a, _ in a })
        let picked = model.query.picked(.trip)
        let shown = model.trips.filter { filter.isEmpty || $0.title.localizedCaseInsensitiveContains(filter) }
        return List(shown) { trip in
            Button { Haptics.tick(); model.toggle(.trip, String(trip.id)) } label: {
                HStack(spacing: 12) {
                    Thumb(sha: trip.cover).frame(width: 64, height: 64).clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(trip.title.replacingOccurrences(of: "Trip to ", with: "")).font(.serif(17, .medium)).foregroundStyle(.primary)
                        Text("\(Dates.span(trip.start, trip.end)) · \((found[String(trip.id)] ?? 0).formatted()) of \(trip.n.formatted())")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Image(systemName: "checkmark").fontWeight(.semibold).opacity(picked.contains(String(trip.id)) ? 1 : 0)
                }
                .opacity(found[String(trip.id)] == nil ? 0.5 : 1)
            }
        }
        .listStyle(.plain)
        .searchable(text: $filter, placement: .navigationBarDrawer(displayMode: .always), prompt: "Filter")
        .overlay { if model.trips.isEmpty { ContentUnavailableView("No trips yet", systemImage: "airplane") } }
    }

    // MARK: years and months

    private var years: some View {
        let byYear = Dictionary(grouping: months, by: { $0.ym / 12 })
        let from = model.query.from, to = model.query.to
        return List(byYear.keys.sorted(by: >), id: \.self) { year in
            let total = byYear[year]!.reduce(0) { $0 + $1.count }
            let whole = from == year * 12 && to == year * 12 + 11
            DisclosureGroup(isExpanded: Binding(get: { open == year }, set: { open = $0 ? year : nil })) {
                ForEach(byYear[year]!, id: \.ym) { m in
                    Button { pick(m.ym, m.ym) } label: {
                        HStack {
                            Text(Dates.monthName(m.ym)).foregroundStyle(.primary)
                            Spacer()
                            Text(m.count.formatted()).foregroundStyle(.secondary).monospacedDigit()
                            Image(systemName: "checkmark").fontWeight(.semibold).opacity(from == m.ym && to == m.ym ? 1 : 0)
                        }
                    }
                }
            } label: {
                Button { pick(year * 12, year * 12 + 11) } label: {
                    HStack {
                        Text(String(year)).font(.serif(20)).foregroundStyle(.primary)
                        Spacer()
                        Text(total.formatted()).foregroundStyle(.secondary).monospacedDigit()
                        Image(systemName: "checkmark").fontWeight(.semibold).opacity(whole ? 1 : 0)
                    }
                }
                .buttonStyle(.borderless)
            }
        }
        .listStyle(.plain)
    }

    private func pick(_ a: Int, _ b: Int) {
        var q = model.query
        if q.from == a && q.to == b { q.from = nil; q.to = nil } else { q.from = a; q.to = b }
        model.query = q
    }
}

/// A thumbnail in SwiftUI, from the phone's store.
struct Thumb: View {
    let sha: String
    @State private var image: UIImage?

    var body: some View {
        ZStack {
            Color.well
            if let image { Image(uiImage: image).resizable().scaledToFill() }
        }
        .clipped()
        .task(id: sha) { image = sha.isEmpty ? nil : await ImageStore.shared.thumb(sha) }
    }
}
