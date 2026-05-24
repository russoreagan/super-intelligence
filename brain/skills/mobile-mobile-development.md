---
name: mobile-development
description: Use when building cross-platform mobile apps with React Native or Flutter, or native apps for iOS (SwiftUI) and Android (Jetpack Compose). Covers navigation, state management, and platform patterns.
summary: Mobile development with React Native, Flutter, SwiftUI, and Jetpack Compose including navigation, state management, and platform-specific patterns.
triggers: [React Native, Flutter, SwiftUI, Jetpack Compose, mobile, iOS, Android, cross-platform, navigation]
disable-model-invocation: true

---
# Mobile Development (Unified)

## Goal
Build high-quality mobile applications using cross-platform or native frameworks with proper architecture and platform conventions.

## When to Use
- Building cross-platform mobile apps
- Developing native iOS or Android apps
- Implementing mobile navigation
- Managing mobile app state
- Optimizing mobile performance
- Following platform design guidelines

## Framework Comparison

| Framework     | Language          | Performance | Code Sharing | Best For                |
| ------------- | ----------------- | ----------- | ------------ | ----------------------- |
| React Native  | JavaScript/TS     | Near-native | ~95%         | JS teams, rapid dev     |
| Flutter       | Dart              | Near-native | ~95%         | Custom UI, animations   |
| SwiftUI       | Swift             | Native      | iOS only     | iOS-first apps          |
| Jetpack Compose| Kotlin           | Native      | Android only | Android-first apps      |

## React Native

### Project Setup
```bash
# Create new project with Expo
npx create-expo-app@latest MyApp --template tabs

# Or bare React Native
npx react-native@latest init MyApp
```

### Navigation (React Navigation)
```typescript
// App.tsx
import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';

type RootStackParamList = {
  Home: undefined;
  Details: { itemId: number };
  Profile: { userId: string };
};

const Stack = createNativeStackNavigator<RootStackParamList>();
const Tab = createBottomTabNavigator();

function HomeStack() {
  return (
    <Stack.Navigator>
      <Stack.Screen name="Home" component={HomeScreen} />
      <Stack.Screen 
        name="Details" 
        component={DetailsScreen}
        options={({ route }) => ({ title: `Item ${route.params.itemId}` })}
      />
    </Stack.Navigator>
  );
}

function App() {
  return (
    <NavigationContainer>
      <Tab.Navigator>
        <Tab.Screen name="HomeTab" component={HomeStack} />
        <Tab.Screen name="Settings" component={SettingsScreen} />
      </Tab.Navigator>
    </NavigationContainer>
  );
}
```

### State Management (Zustand)
```typescript
// store/userStore.ts
import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import AsyncStorage from '@react-native-async-storage/async-storage';

interface UserState {
  user: User | null;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

export const useUserStore = create<UserState>()(
  persist(
    (set) => ({
      user: null,
      isLoading: false,
      login: async (email, password) => {
        set({ isLoading: true });
        try {
          const user = await api.login(email, password);
          set({ user, isLoading: false });
        } catch (error) {
          set({ isLoading: false });
          throw error;
        }
      },
      logout: () => set({ user: null }),
    }),
    {
      name: 'user-storage',
      storage: createJSONStorage(() => AsyncStorage),
    }
  )
);
```

### Styling
```typescript
import { StyleSheet, View, Text, useColorScheme } from 'react-native';

function ThemedCard({ title, children }: { title: string; children: React.ReactNode }) {
  const colorScheme = useColorScheme();
  const isDark = colorScheme === 'dark';

  return (
    <View style={[styles.card, isDark && styles.cardDark]}>
      <Text style={[styles.title, isDark && styles.titleDark]}>{title}</Text>
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: '#ffffff',
    borderRadius: 12,
    padding: 16,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.1,
    shadowRadius: 4,
    elevation: 3, // Android shadow
  },
  cardDark: {
    backgroundColor: '#1c1c1e',
  },
  title: {
    fontSize: 18,
    fontWeight: '600',
    color: '#000000',
  },
  titleDark: {
    color: '#ffffff',
  },
});
```

## Flutter

### Project Setup
```bash
flutter create my_app
cd my_app
flutter run
```

### Navigation (GoRouter)
```dart
// lib/router.dart
import 'package:go_router/go_router.dart';

final router = GoRouter(
  initialLocation: '/',
  routes: [
    GoRoute(
      path: '/',
      builder: (context, state) => const HomeScreen(),
      routes: [
        GoRoute(
          path: 'details/:id',
          builder: (context, state) {
            final id = state.pathParameters['id']!;
            return DetailsScreen(itemId: id);
          },
        ),
      ],
    ),
    GoRoute(
      path: '/profile/:userId',
      builder: (context, state) {
        final userId = state.pathParameters['userId']!;
        return ProfileScreen(userId: userId);
      },
    ),
  ],
  errorBuilder: (context, state) => ErrorScreen(error: state.error),
);

// Usage
context.go('/details/123');
context.push('/profile/user456');
context.pop();
```

### State Management (Riverpod)
```dart
// lib/providers/user_provider.dart
import 'package:flutter_riverpod/flutter_riverpod.dart';

class User {
  final String id;
  final String name;
  final String email;
  
  User({required this.id, required this.name, required this.email});
}

class UserNotifier extends StateNotifier<AsyncValue<User?>> {
  UserNotifier() : super(const AsyncValue.data(null));

  Future<void> login(String email, String password) async {
    state = const AsyncValue.loading();
    try {
      final user = await _authService.login(email, password);
      state = AsyncValue.data(user);
    } catch (e, st) {
      state = AsyncValue.error(e, st);
    }
  }

  void logout() {
    state = const AsyncValue.data(null);
  }
}

final userProvider = StateNotifierProvider<UserNotifier, AsyncValue<User?>>((ref) {
  return UserNotifier();
});

// Usage in widget
class ProfileWidget extends ConsumerWidget {
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final userAsync = ref.watch(userProvider);
    
    return userAsync.when(
      data: (user) => user != null 
        ? Text('Hello, ${user.name}') 
        : const Text('Please login'),
      loading: () => const CircularProgressIndicator(),
      error: (e, _) => Text('Error: $e'),
    );
  }
}
```

### Theming
```dart
// lib/theme.dart
import 'package:flutter/material.dart';

class AppTheme {
  static ThemeData lightTheme = ThemeData(
    useMaterial3: true,
    colorScheme: ColorScheme.fromSeed(
      seedColor: Colors.blue,
      brightness: Brightness.light,
    ),
    appBarTheme: const AppBarTheme(
      elevation: 0,
      centerTitle: true,
    ),
    cardTheme: CardTheme(
      elevation: 2,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(8),
      ),
    ),
  );

  static ThemeData darkTheme = ThemeData(
    useMaterial3: true,
    colorScheme: ColorScheme.fromSeed(
      seedColor: Colors.blue,
      brightness: Brightness.dark,
    ),
    // ... same customizations
  );
}

// main.dart
MaterialApp.router(
  theme: AppTheme.lightTheme,
  darkTheme: AppTheme.darkTheme,
  themeMode: ThemeMode.system,
  routerConfig: router,
);
```

## SwiftUI (iOS)

### Navigation
```swift
// ContentView.swift
import SwiftUI

struct ContentView: View {
    @State private var navigationPath = NavigationPath()
    
    var body: some View {
        NavigationStack(path: $navigationPath) {
            List {
                ForEach(items) { item in
                    NavigationLink(value: item) {
                        Text(item.title)
                    }
                }
            }
            .navigationTitle("Items")
            .navigationDestination(for: Item.self) { item in
                ItemDetailView(item: item)
            }
            .navigationDestination(for: User.self) { user in
                UserProfileView(user: user)
            }
        }
    }
}

// Tab navigation
struct MainTabView: View {
    var body: some View {
        TabView {
            HomeView()
                .tabItem {
                    Label("Home", systemImage: "house")
                }
            
            SearchView()
                .tabItem {
                    Label("Search", systemImage: "magnifyingglass")
                }
            
            ProfileView()
                .tabItem {
                    Label("Profile", systemImage: "person")
                }
        }
    }
}
```

### State Management
```swift
// UserStore.swift
import SwiftUI
import Combine

@MainActor
class UserStore: ObservableObject {
    @Published var user: User?
    @Published var isLoading = false
    @Published var error: Error?
    
    func login(email: String, password: String) async {
        isLoading = true
        defer { isLoading = false }
        
        do {
            user = try await authService.login(email: email, password: password)
        } catch {
            self.error = error
        }
    }
    
    func logout() {
        user = nil
    }
}

// Usage
struct LoginView: View {
    @EnvironmentObject var userStore: UserStore
    @State private var email = ""
    @State private var password = ""
    
    var body: some View {
        Form {
            TextField("Email", text: $email)
            SecureField("Password", text: $password)
            
            Button("Login") {
                Task {
                    await userStore.login(email: email, password: password)
                }
            }
            .disabled(userStore.isLoading)
        }
    }
}
```

## Jetpack Compose (Android)

### Navigation
```kotlin
// Navigation.kt
@Composable
fun AppNavigation() {
    val navController = rememberNavController()
    
    NavHost(navController = navController, startDestination = "home") {
        composable("home") {
            HomeScreen(
                onItemClick = { itemId ->
                    navController.navigate("details/$itemId")
                }
            )
        }
        
        composable(
            "details/{itemId}",
            arguments = listOf(navArgument("itemId") { type = NavType.StringType })
        ) { backStackEntry ->
            val itemId = backStackEntry.arguments?.getString("itemId") ?: return@composable
            DetailsScreen(itemId = itemId)
        }
        
        composable(
            "profile/{userId}",
            arguments = listOf(navArgument("userId") { type = NavType.StringType })
        ) { backStackEntry ->
            val userId = backStackEntry.arguments?.getString("userId") ?: return@composable
            ProfileScreen(userId = userId)
        }
    }
}
```

### State Management (ViewModel)
```kotlin
// UserViewModel.kt
@HiltViewModel
class UserViewModel @Inject constructor(
    private val authRepository: AuthRepository
) : ViewModel() {
    
    private val _uiState = MutableStateFlow<UserUiState>(UserUiState.LoggedOut)
    val uiState: StateFlow<UserUiState> = _uiState.asStateFlow()
    
    fun login(email: String, password: String) {
        viewModelScope.launch {
            _uiState.value = UserUiState.Loading
            try {
                val user = authRepository.login(email, password)
                _uiState.value = UserUiState.LoggedIn(user)
            } catch (e: Exception) {
                _uiState.value = UserUiState.Error(e.message ?: "Unknown error")
            }
        }
    }
    
    fun logout() {
        _uiState.value = UserUiState.LoggedOut
    }
}

sealed class UserUiState {
    object LoggedOut : UserUiState()
    object Loading : UserUiState()
    data class LoggedIn(val user: User) : UserUiState()
    data class Error(val message: String) : UserUiState()
}

// Usage in Composable
@Composable
fun ProfileScreen(viewModel: UserViewModel = hiltViewModel()) {
    val uiState by viewModel.uiState.collectAsState()
    
    when (val state = uiState) {
        is UserUiState.Loading -> CircularProgressIndicator()
        is UserUiState.LoggedIn -> UserProfile(user = state.user)
        is UserUiState.Error -> ErrorMessage(message = state.message)
        is UserUiState.LoggedOut -> LoginPrompt()
    }
}
```

### Theming
```kotlin
// Theme.kt
private val LightColors = lightColorScheme(
    primary = Color(0xFF1976D2),
    onPrimary = Color.White,
    secondary = Color(0xFF03DAC6),
    surface = Color.White,
    background = Color(0xFFFAFAFA),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFF90CAF9),
    onPrimary = Color.Black,
    secondary = Color(0xFF03DAC6),
    surface = Color(0xFF121212),
    background = Color(0xFF121212),
)

@Composable
fun AppTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit
) {
    val colorScheme = if (darkTheme) DarkColors else LightColors
    
    MaterialTheme(
        colorScheme = colorScheme,
        typography = Typography,
        content = content
    )
}
```

## Platform Guidelines

### iOS Human Interface Guidelines
- Use SF Symbols for icons
- Follow safe area insets
- Support Dynamic Type
- Use system colors for accessibility
- Implement haptic feedback

### Android Material Design
- Follow Material 3 design system
- Support dark theme
- Use Material icons
- Implement edge-to-edge display
- Support different screen sizes

## Implementation Checklist
- [ ] Navigation structure defined
- [ ] State management solution chosen
- [ ] Theming supports light/dark modes
- [ ] Platform design guidelines followed
- [ ] Accessibility implemented (VoiceOver/TalkBack)
- [ ] Deep linking configured
- [ ] Offline support considered
- [ ] Performance optimized (list virtualization)
- [ ] Error handling with user feedback
- [ ] Analytics and crash reporting integrated
